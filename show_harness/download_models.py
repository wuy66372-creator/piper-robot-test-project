"""Download pinned official backbones/adapters and verify their published LFS hashes."""
import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download

ROOT = Path(__file__).resolve().parent
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "hackathon"
STORE = Path(os.environ.get("SHOWHARNESS_MODEL_HOME", CACHE / "show-harness-models"))


def fetch(repo, revision, destination, patterns=None, *, verify_only=False):
    info = HfApi().model_info(repo, revision=revision, files_metadata=True)
    if not verify_only:
        print(f"Downloading {repo}@{revision} -> {destination}", flush=True)
        snapshot_download(repo, revision=revision, local_dir=destination,
                          allow_patterns=patterns, max_workers=4)
    else:
        print(f"Verifying {repo}@{revision} -> {destination}", flush=True)
    records = []
    for entry in info.siblings:
        if patterns and not any(entry.rfilename.startswith(p[:-1]) for p in patterns):
            continue
        path = destination / entry.rfilename
        if not path.is_file() or path.stat().st_size != entry.size:
            raise RuntimeError(f"Missing or truncated: {path}")
        record = {"file": entry.rfilename, "bytes": entry.size}
        if entry.lfs:
            with path.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            if digest != entry.lfs.sha256:
                raise RuntimeError(f"SHA-256 mismatch: {path}")
            record["sha256"] = digest
        records.append(record)
    return {"repo": repo, "revision": revision, "directory": str(destination), "files": records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true",
                        help="Verify local files against pinned official metadata without downloading weights")
    args = parser.parse_args()
    spec = json.loads((ROOT / "models.lock.json").read_text())
    records = []
    # Adapters first; their configs must match the declared backbone before a large download.
    records.append(fetch(spec["adapter_repo"], spec["adapter_revision"],
                         STORE / "adapters", [f"{a}/*" for a in spec["adapters"]],
                         verify_only=args.verify_only))
    for adapter in spec["adapters"]:
        base_key = adapter.removesuffix("_sim")
        config = json.loads((STORE / "adapters" / adapter / "adapter_config.json").read_text())
        if config["base_model_name_or_path"] != spec["models"][base_key]["repo"]:
            raise RuntimeError(f"Adapter/base mismatch: {adapter}")
    for model in spec["models"].values():
        records.append(fetch(model["repo"], model["revision"], STORE / "huggingface" / "hub" / model["repo"],
                             verify_only=args.verify_only))
    report = {"verified_at": datetime.now(timezone.utc).isoformat(), "downloads": records}
    STORE.mkdir(parents=True, exist_ok=True)
    (STORE / "download_receipt.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"All weights verified. Receipt: {STORE / 'download_receipt.json'}", flush=True)


if __name__ == "__main__":
    main()
