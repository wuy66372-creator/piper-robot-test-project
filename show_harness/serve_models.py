"""Serve official action LoRAs with the upstream template and a 16 GB GPU budget."""
import argparse
import json
import os
from pathlib import Path
import shlex
import socket
import sys
import sysconfig
from model_profiles import resolve

ROOT = Path(__file__).resolve().parents[1]
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "hackathon"
STORE = Path(os.environ.get("SHOWHARNESS_MODEL_HOME", CACHE / "show-harness-models"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", nargs="?", default="qwen3_5_2b")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    selected=resolve(args.profile)
    base=selected['base']
    spec = json.loads((ROOT / "show_harness/models.lock.json").read_text())
    model_id = spec["models"][base]["repo"]
    model = STORE / "huggingface/hub" / model_id
    index = json.loads((model / "model.safetensors.index.json").read_text())
    for shard in set(index["weight_map"].values()):
        if not (model / shard).is_file():
            raise SystemExit(f"Missing shard: {model / shard}; run models.sh download")
    profiles = [] if selected.get('adapter') else [base]
    if base == "qwen3_5_2b" and not selected.get('adapter'):
        profiles.append("qwen3_5_2b_sim")
    adapters = []
    for profile in profiles:
        path = STORE / "adapters" / profile
        config = json.loads((path / "adapter_config.json").read_text())
        if config["base_model_name_or_path"] != model_id or config["r"] != 64:
            raise SystemExit(f"Unexpected adapter configuration: {path}")
        if not (path / "adapter_model.safetensors").is_file():
            raise SystemExit(f"Missing adapter: {path}")
        name = (profile.removesuffix("_sim") + "_showharness_sim"
                if profile.endswith("_sim") else profile + "_showharness_ft")
        adapters.append(f"{name}={path}")
    if selected.get('adapter'):
        adapters.append(selected['model']+'='+selected['adapter'])
    command = [sys.executable, "-m", "vllm.entrypoints.cli.main", "serve", str(model),
               "--served-model-name", model_id, "--host", "127.0.0.1", "--port", str(args.port),
               "--dtype", "bfloat16", "--tensor-parallel-size", "1",
               "--gpu-memory-utilization", "0.86" if base.endswith("4b") else "0.80",
               "--max-model-len", "4096", "--max-num-seqs", "1",
               "--kv-cache-memory-bytes", "536870912",
               "--max-num-batched-tokens", "4096", "--enforce-eager",
               "--limit-mm-per-prompt", json.dumps({'image':3 if selected['arm']=='dual' else 2,'video':0}),
               "--mm-processor-kwargs", '{"max_pixels":262144}',
               "--override-generation-config", '{"temperature":0,"top_p":1.0,"top_k":-1}',
               "--chat-template", str(ROOT / "Show-Harness/models/chat_templates/qwen3_5_nothink.jinja"),
               "--enable-lora", "--max-lora-rank", "64", "--max-loras", str(len(adapters)),
               "--max-cpu-loras", str(len(adapters)), "--lora-modules", *adapters]
    print(shlex.join(command), flush=True)
    if args.dry_run:
        return
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", args.port)) == 0:
            raise SystemExit(f"Port {args.port} is occupied; stop its owner or choose --port")
    os.environ.setdefault("HF_HOME", str(STORE / "huggingface"))
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")
    cuda = Path(sysconfig.get_paths()["purelib"]) / "nvidia/cu13"
    if (cuda / "bin/nvcc").is_file():
        os.environ.setdefault("CUDA_HOME", str(cuda))
        os.environ["PATH"] = str(cuda / "bin") + os.pathsep + os.environ.get("PATH", "")
    # Greedy, one-request action decoding does not need the optional FlashInfer
    # sampling JIT, which otherwise assumes a system-wide CUDA development SDK.
    os.environ.setdefault("VLLM_USE_FLASHINFER_SAMPLER", "0")
    os.execv(sys.executable, command)


if __name__ == "__main__":
    main()
