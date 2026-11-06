"""Offline-image inference check through Show-Harness; never imports robot drivers."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

import numpy as np
from PIL import Image
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Show-Harness"))
from core.config import load_yaml, resolve_vlm_config
from core.vlm.mvtoken_roles import MvTokenController
from core.vlm.vlm_client import VLMClient


def gpu_memory():
    return subprocess.check_output([
        "nvidia-smi", "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader"
    ], text=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="qwen3_5_2b",
                        choices=("qwen3_5_0_8b", "qwen3_5_2b", "qwen3_5_4b", "qwen3_5_2b_sim"))
    parser.add_argument("--url", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    config = resolve_vlm_config(load_yaml(ROOT / "show_harness/vlm_local.yaml"), args.profile)
    config["base_url"] = args.url
    registry = requests.get(args.url + "/models", timeout=10)
    registry.raise_for_status()
    if config["model"] not in {item["id"] for item in registry.json()["data"]}:
        raise RuntimeError(f"Adapter {config['model']} is not registered on {args.url}")
    client = VLMClient(**{k: config[k] for k in (
        "base_url", "model", "api_key", "timeout_s", "max_tokens", "temperature",
        "chat_template_kwargs", "provider", "max_retries")})
    prompt = (ROOT / "Show-Harness/prompts/v3/mvtoken_generator_lite.txt").read_text()
    controller = MvTokenController(client, prompt)
    sample_dir = ROOT / "results/show_harness/models/samples"
    source = json.loads((sample_dir / "sources.json").read_text())
    selected_split = "sim" if args.profile.endswith("_sim") else "real"
    checks = []
    for sample in source["samples"]:
        if sample["split"] != selected_split:
            continue
        images = []
        for filename, original in zip(sample["local_images"], sample["source_images"]):
            path = sample_dir / filename
            if hashlib.sha256(path.read_bytes()).hexdigest() != original["sha256"]:
                raise RuntimeError(f"Changed source image: {path}")
            images.append(np.asarray(Image.open(path).convert("RGB")))
        instruction = sample["sample"]["instruction"]
        task = re.search(r"^Task: (.+)$", instruction, re.MULTILINE).group(1)
        recent = re.search(r"^Recent moves, newest first: (.+)$", instruction, re.MULTILINE).group(1)
        for repeat in range(2):
            response = controller.decide(task, "unknown", recent, images[0], images[1], debug=True)
            # No guided decoder or parser recovery may conceal a wrong output shape.
            raw = response.raw_text.strip()
            if raw not in controller.allowed_tokens or raw != response.token:
                raise RuntimeError(f"Model did not emit one exact action token: {raw!r}")
            record = {"episode": sample["episode"], "repeat": repeat,
                      "raw_token": raw, "reference_token": sample["sample"]["output"],
                      "response": response.payload, "gpu": gpu_memory(),
                      "image_files": sample["local_images"]}
            checks.append(record)
            print(f"{args.profile}: {sample['episode']['episode']} repeat={repeat} -> {raw}; "
                  f"{record['gpu']}", flush=True)
    if not checks:
        raise RuntimeError("No offline samples selected")
    now = datetime.now(timezone(timedelta(hours=8)))
    output = args.output or ROOT / "results/show_harness/models" / f"{now:%Y%m%d-%H%M%S}-{args.profile}.json"
    report = {"checked_at": now.isoformat(), "profile": args.profile, "model": config["model"],
              "source_repo": source["repo"], "source_revision": source["revision"],
              "validation_scope": "Official saved training samples; image API, template and unconstrained token output only. No hardware or task-success validation.",
              "registered_models": registry.json(), "checks": checks}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"PASS: {output}", flush=True)


if __name__ == "__main__":
    main()
