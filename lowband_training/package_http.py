"""Create an offline model-directory payload; never publish/register implicitly."""
import argparse
import hashlib
import json
from pathlib import Path
import tarfile
import tempfile

from .http_transport import reference


def build(model, data, wheels, output):
    sources = {}
    source_dir = Path(__file__).parent
    names = ("__init__.py", "http_transport.py", "http_worker.py", "http_controller.py", "hdfs_store.py",
             "storage.py", "protocol.py", "train.py", "http_service_entry.sh", "http_probe.py", "dorado_discovery.py", "requirements.txt",
             "evaluate.py", "http_evaluate.py", "cuda_bootstrap.py", "spot_task_endpoints.py", "hdfs_mounted_reader.py",
             "dorado_services.json", "dorado_http_training.ipynb", "HTTP_DEPLOYMENT.md")
    for name in names:
        sources["lowband_training/" + name] = source_dir / name
    for path in Path(model).iterdir():
        if path.is_file():
            sources["assets/model/" + path.name] = path
    for name in ("manifest.json", "tokens.pt"):
        sources["assets/data/" + name] = Path(data) / name
    for path in Path(wheels).glob("*.whl"):
        sources["wheels/" + path.name] = path
    if not any(name.startswith("wheels/") for name in sources):
        raise ValueError("An offline wheelhouse is required")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lowband-package-") as temp:
        config = json.loads((source_dir / "qwen_config.deployment.json").read_text())
        config["model_path"] = "assets/model"
        config["data_manifest"] = "assets/data/manifest.json"
        config_path = Path(temp) / "config.json"
        config_path.write_text(json.dumps(config, indent=2) + "\n")
        sources["config.json"] = config_path
        manifest = {"schema": 1, "runtime": "CPython 3.11 Linux x86_64; platform torch >=2.2",
                    "gpu_network": "inbound HTTP only; no direct HDFS/NAS or package downloads",
                    "files": {name: reference(path) for name, path in sources.items()}}
        manifest_path = Path(temp) / "package-manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        sources["package-manifest.json"] = manifest_path
        with tarfile.open(output, "w:gz", compresslevel=1) as archive:
            for name, path in sorted(sources.items()):
                archive.add(path, arcname=name, recursive=False)
    return {"archive": str(output.resolve()), **reference(output), "files": len(sources),
            "published": False, "gpu_runtime_verified": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("model", "data", "wheels", "output"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    result = build(args.model, args.data, args.wheels, args.output)
    Path(args.output + ".json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
