"""Optional CPU-side HDFS read bridge; writes retain the standard HDFS CLI.

Use only with an independently verified URI-to-mount mapping. HdfsStore still
checks every downloaded blob's size and SHA256 before accepting the copy.
"""
import argparse
import os
from pathlib import Path, PurePosixPath
import shutil
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri-prefix", required=True)
    parser.add_argument("--mount-prefix", required=True)
    parser.add_argument("--parent-blob-mount", help="Read-only inherited blobs of an explicit fork parent")
    args, command = parser.parse_known_args()
    prefix = args.uri_prefix.rstrip("/") + "/"
    mount = Path(args.mount_prefix).resolve()
    if (len(command) == 3 and command[0] == "-get" and command[1].startswith(prefix)):
        relative = PurePosixPath(command[1][len(prefix):])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("HDFS read outside verified mount mapping")
        source = mount.joinpath(*relative.parts)
        if not source.resolve().is_relative_to(mount):
            raise ValueError("Mounted source escapes the verified root")
        if not source.is_file() and args.parent_blob_mount:
            import re
            if len(relative.parts) == 2 and relative.parts[0] == "blobs" and re.fullmatch(
                r"[0-9a-f]{64}\.pt", relative.parts[1]
            ):
                parent = Path(args.parent_blob_mount).resolve()
                source = parent / relative.parts[1]
                if not source.resolve().is_relative_to(parent):
                    raise ValueError("Inherited blob escapes the verified parent")
        if source.is_file():
            with source.open("rb") as src, Path(command[2]).open("xb") as dst:
                shutil.copyfileobj(src, dst, 4 << 20)
                dst.flush()
                os.fsync(dst.fileno())
            return
    # All metadata, writes, renames, and unmapped reads use the ordinary client.
    os.execvp("hdfs", ["hdfs", "dfs", *command])


if __name__ == "__main__":
    main()
