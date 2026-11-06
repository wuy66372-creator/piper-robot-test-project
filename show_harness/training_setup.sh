#!/usr/bin/env bash
set -euo pipefail
task_cache="${XDG_CACHE_HOME:-$HOME/.cache}/hackathon"
task_lf="$task_cache/llamafactory-9ce6b663"
task_env="$task_cache/show-harness-train-venv"
task_uv="$HOME/.local/bin/uv"
if [[ ! -d "$task_lf/.git" ]]; then
  mkdir -p "$task_lf"
  git init -q "$task_lf"
  git -C "$task_lf" remote add origin https://github.com/hiyouga/LLaMA-Factory.git
  git -C "$task_lf" fetch --depth 1 origin 9ce6b663e9d87cd3c0cb42a1d3ff5cdfe292426d
  git -C "$task_lf" checkout --detach FETCH_HEAD
fi
[[ $(git -C "$task_lf" rev-parse HEAD) == 9ce6b663e9d87cd3c0cb42a1d3ff5cdfe292426d ]]
[[ -x "$task_env/bin/python" ]] || "$task_uv" venv --python /usr/bin/python3.12 "$task_env"
# Reuse already installed immutable GPU libraries with independent directory entries.
# Hard links survive an inference-env upgrade without modifying the original env.
task_gpu_site="$task_cache/show-harness-vllm-venv/lib/python3.12/site-packages"
task_train_site="$task_env/lib/python3.12/site-packages"
if [[ -d "$task_gpu_site/torch" && ! -d "$task_train_site/torch" ]]; then
  while IFS= read -r -d '' task_package; do
    cp -al "$task_package" "$task_train_site/"
  done < <(find "$task_gpu_site" -mindepth 1 -maxdepth 1 -type d \( -name 'torch*' -o -name 'nvidia*' -o -name 'cuda*' -o -name 'triton*' -o -name 'functorch' \) -print0)
fi
while IFS= read -r -d '' task_package; do
  task_name="$(basename "$task_package")"
  [[ -e "$task_train_site/$task_name" ]] || cp -al "$task_package" "$task_train_site/"
done < <(find "$task_gpu_site" -mindepth 1 -maxdepth 1 ! -name 'transformers*' ! -name 'vllm*' ! -name '*.pth' ! -name '_virtualenv*' ! -name '__pycache__' -print0)
# The upstream WebUI is not used: install the CLI trainer and its actual training deps.
# Never seed compiled Arrow wheels from other Python ABIs.
"$task_uv" pip install --python "$task_env/bin/python" --no-deps -e "$task_lf"
"$task_uv" pip install --python "$task_env/bin/python" --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
  'transformers==5.7.0' 'peft==0.18.1' 'accelerate==1.11.0' 'datasets==4.0.0' \
  'trl==0.24.0' 'torchdata==0.11.0' bitsandbytes 'tyro<0.9' einops \
  sentencepiece tiktoken omegaconf fire 'pandas<3' matplotlib
"$task_env/bin/python" -c 'import torch,transformers,peft,llamafactory; print(torch.__version__,transformers.__version__)'
"$task_uv" pip freeze --python "$task_env/bin/python" > "$task_cache/show-harness-training.lock.txt"
"$task_env/bin/python" "$(dirname "$0")/training_env_check.py"
