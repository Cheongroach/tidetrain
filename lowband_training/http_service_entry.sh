#!/usr/bin/env bash
set -euo pipefail
# This script is shipped INSIDE the model package. No runtime HDFS/NAS access.
package="${LOWBAND_PACKAGE_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"
zone="${LOWBAND_ZONE:?set the fixed worker slot}"
port="${LOWBAND_HTTP_PORT:-${PORT0:-}}"
[[ "$zone" =~ ^[A-Za-z0-9_-]+$ && "$port" =~ ^[0-9]+$ ]] || exit 2
: "${LOWBAND_HTTP_TOKEN:?set the per-run HTTP secret on both worker and controller}"
python3 - "$package" <<'VERIFY'
import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
manifest = json.loads((root / 'package-manifest.json').read_text())
for name, ref in manifest['files'].items():
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise ValueError('Package path outside root')
    digest = hashlib.sha256()
    with path.open('rb') as src:
        while block := src.read(4 << 20):
            digest.update(block)
    if digest.hexdigest() != ref['sha256'] or path.stat().st_size != ref['bytes']:
        raise ValueError('Package checksum mismatch: ' + name)
import torch
assert sys.version_info[:2] == (3, 11), 'This wheel bundle targets Python 3.11'
assert tuple(map(int, torch.__version__.split('+')[0].split('.')[:2])) >= (2, 2)
VERIFY
local_root="${LOWBAND_LOCAL_ROOT:-/opt/lowband-local-$zone}"
mkdir -p "$local_root"
runtime="$local_root/venv"
python3 -m venv --without-pip --system-site-packages "$runtime"
python3 -m pip --python "$runtime/bin/python" install --no-index --no-deps --ignore-installed \
    "$package"/wheels/*.whl
export PYTHONNOUSERSITE=1
export PYTHONPATH="$package${PYTHONPATH:+:$PYTHONPATH}"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false
exec "$runtime/bin/python" -m lowband_training.cuda_bootstrap "$local_root/cuda-report.json" \
    "$runtime/bin/python" -m lowband_training.http_worker serve \
    --config "$package/config.json" --worker "$zone" --assets "$package/assets" \
    --root "$local_root/worker" --host "${LOWBAND_HTTP_HOST:-::}" --port "$port" \
    --device cuda --max-seconds "${LOWBAND_SERVICE_SECONDS:-10740}"
