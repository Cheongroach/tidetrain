"""Transactional CPU authority: leases, fencing, round CAS and an event tape.

SQLite lives on ONE gateway's durable local disk (not NFS/HDFS). Multiple
processes may use this file on that host; remote clients use HTTP. This is
crash recovery and single-authority serialization, not multi-host consensus.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from .storage import canonical, IntegrityError


class Rejected(IntegrityError):
    pass


class Ledger:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS leases(
                    scope TEXT PRIMARY KEY, owner TEXT NOT NULL, fence INTEGER NOT NULL, expiry REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS rounds(version INTEGER PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS updates(
                    version INTEGER, worker TEXT, value TEXT NOT NULL, PRIMARY KEY(version,worker));
                CREATE TABLE IF NOT EXISTS recovery(sha256 TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL,
                    kind TEXT NOT NULL, value TEXT NOT NULL);
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def event(db, kind, value):
        db.execute("INSERT INTO events(timestamp,kind,value) VALUES(?,?,?)",
                   (time.time(), kind, canonical(value).decode()))

    def initialize(self, config, state):
        encoded = canonical(config).decode()
        with self.transaction() as db:
            previous = db.execute("SELECT value FROM meta WHERE key='config'").fetchone()
            if previous:
                if previous[0] != encoded:
                    raise Rejected("Run configuration changed")
                return
            if not config["workers"] or len(set(config["workers"])) != len(config["workers"]):
                raise ValueError("Unique members required")
            if not 1 <= config["quorum"] <= len(config["workers"]):
                raise ValueError("Invalid quorum")
            db.execute("INSERT INTO meta VALUES('config',?)", (encoded,))
            initial = {"round": 0, "epoch": 1, "state": state, "workers": {},
                       "effective_tokens": 0, "accepted": [], "created_at": time.time()}
            db.execute("INSERT INTO rounds VALUES(0,?)", (canonical(initial).decode(),))
            self.event(db, "initialized", {"members": config["workers"], "epoch": 1})

    def config(self):
        with self.connect() as db:
            return json.loads(db.execute("SELECT value FROM meta WHERE key='config'").fetchone()[0])

    @staticmethod
    def current(db):
        return json.loads(db.execute("SELECT value FROM rounds ORDER BY version DESC LIMIT 1").fetchone()[0])

    def snapshot(self):
        with self.connect() as db:
            return self.current(db)

    def worker_snapshot(self, worker):
        with self.connect() as db:
            base = self.current(db)
            info = base["workers"].get(worker)
            base["workers"] = {}
            if info:
                hydrated = dict(info)
                if "recovery_sha256" in hydrated:
                    row = db.execute("SELECT value FROM recovery WHERE sha256=?",
                                     (hydrated.pop("recovery_sha256"),)).fetchone()
                    hydrated["recovery"] = json.loads(row[0])
                base["workers"][worker] = hydrated
            return base

    def acquire(self, scope, owner, seconds=120):
        if not owner or not 0 < seconds <= 600:
            raise ValueError("Invalid lease")
        with self.transaction() as db:
            config = json.loads(db.execute("SELECT value FROM meta WHERE key='config'").fetchone()[0])
            if scope != "coordinator" and scope not in {"worker:" + w for w in config["workers"]}:
                raise Rejected("Unknown lease scope")
            now = time.time()
            previous = db.execute("SELECT owner,fence,expiry FROM leases WHERE scope=?", (scope,)).fetchone()
            if previous and previous[2] > now:
                if previous[0] != owner:
                    raise Rejected("Lease held by another incarnation")
                fence = previous[1]
            else:
                fence = previous[1] + 1 if previous else 1
            lease = {"scope": scope, "owner": owner, "fence": fence, "expiry": now + seconds}
            db.execute("INSERT OR REPLACE INTO leases VALUES(?,?,?,?)",
                       (scope, owner, fence, lease["expiry"]))
            self.event(db, "lease_acquired", lease)
            return lease

    @staticmethod
    def check_lease(db, lease):
        row = db.execute("SELECT owner,fence,expiry FROM leases WHERE scope=?", (lease["scope"],)).fetchone()
        if not row or row[0] != lease["owner"] or row[1] != lease["fence"] or row[2] <= time.time():
            raise Rejected("Expired or fenced lease")

    def renew(self, lease, seconds=120):
        if not 0 < seconds <= 600:
            raise ValueError("Invalid lease TTL")
        with self.transaction() as db:
            self.check_lease(db, lease)
            expiry = time.time() + seconds
            db.execute("UPDATE leases SET expiry=? WHERE scope=?", (expiry, lease["scope"]))
            return {**lease, "expiry": expiry}

    @staticmethod
    def state_hash(state):
        return hashlib.sha256(canonical(state)).hexdigest()

    def submit(self, lease, record):
        with self.transaction() as db:
            self.check_lease(db, lease)
            if lease["scope"] != "worker:" + record["worker"]:
                raise Rejected("Worker lease mismatch")
            base = self.current(db)
            if record["base_round"] != base["round"] or record["epoch"] != base["epoch"] or \
                    record["base_sha256"] != self.state_hash(base["state"]):
                raise Rejected("Stale version, epoch or ancestry")
            config = json.loads(db.execute("SELECT value FROM meta WHERE key='config'").fetchone()[0])
            expected_tokens = config["local_steps"] * config["batch_size"] * config["seq_len"]
            old_cursor = base["workers"].get(record["worker"], {}).get("cursor", 0)
            if record["tokens"] != expected_tokens or record["cursor"] != old_cursor + \
                    config["local_steps"] * config["batch_size"]:
                raise Rejected("Token count or cursor mismatch")
            if record["fence"] != lease["fence"]:
                raise Rejected("Update fence mismatch")
            previous = db.execute("SELECT value FROM updates WHERE version=? AND worker=?",
                                  (base["round"], record["worker"])).fetchone()
            encoded = canonical(record).decode()
            if previous:
                if previous[0] != encoded:
                    raise Rejected("Conflicting logical update")
                return False
            db.execute("INSERT INTO updates VALUES(?,?,?)", (base["round"], record["worker"], encoded))
            self.event(db, "submitted", {"round": base["round"], "worker": record["worker"],
                                        "tokens": record["tokens"], "fence": lease["fence"]})
            return True

    def pending(self, base):
        with self.connect() as db:
            rows = db.execute("SELECT worker,value FROM updates WHERE version=?", (base["round"],)).fetchall()
        by_worker = {worker: json.loads(value) for worker, value in rows}
        return [by_worker[w] for w in self.config()["workers"] if w in by_worker]

    def commit(self, lease, base, records, state):
        with self.transaction() as db:
            self.check_lease(db, lease)
            if lease["scope"] != "coordinator":
                raise Rejected("Coordinator lease required")
            current = self.current(db)
            if canonical(current) != canonical(base):
                raise Rejected("Round compare-and-swap failed")
            if not records or len({r["worker"] for r in records}) != len(records):
                raise Rejected("Empty or duplicate acceptance")
            config = json.loads(db.execute("SELECT value FROM meta WHERE key='config'").fetchone()[0])
            if len(records) < config["quorum"]:
                raise Rejected("Quorum not met")
            if len(records) < len(config["workers"]) and time.time() < \
                    base["created_at"] + config["round_timeout"]:
                raise Rejected("Quorum timeout not reached")
            workers = dict(base["workers"])
            for record in records:
                stored = db.execute("SELECT value FROM updates WHERE version=? AND worker=?",
                                    (base["round"], record["worker"])).fetchone()
                if not stored or stored[0] != canonical(record).decode():
                    raise Rejected("Acceptance differs from submitted record")
                recovery_encoded = canonical(record["recovery"]).decode()
                recovery_hash = hashlib.sha256(recovery_encoded.encode()).hexdigest()
                db.execute("INSERT OR IGNORE INTO recovery VALUES(?,?)", (recovery_hash, recovery_encoded))
                workers[record["worker"]] = {"cursor": record["cursor"], "recovery_sha256": recovery_hash}
            total = sum(record["tokens"] for record in records)
            result = {"round": base["round"] + 1, "epoch": base["epoch"], "state": state,
                      "parent_sha256": self.state_hash(base["state"]), "workers": workers,
                      "effective_tokens": base["effective_tokens"] + total,
                      "accepted": [record["worker"] for record in records], "created_at": time.time()}
            db.execute("INSERT INTO rounds VALUES(?,?)", (result["round"], canonical(result).decode()))
            self.event(db, "round_committed", {"round": result["round"], "tokens": total,
                                              "accepted": result["accepted"], "fence": lease["fence"],
                                              "state_sha256": self.state_hash(state)})
            return result

    def events(self):
        with self.connect() as db:
            return [{"seq": seq, "time": stamp, "kind": kind, "value": json.loads(value)}
                    for seq, stamp, kind, value in db.execute(
                        "SELECT seq,timestamp,kind,value FROM events ORDER BY seq")]
