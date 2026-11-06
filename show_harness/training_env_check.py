import importlib.metadata as metadata
import json
from pathlib import Path
from llamafactory.train.tuner import run_exp

versions={p:metadata.version(p) for p in ('torch','transformers','peft','accelerate','datasets','bitsandbytes','llamafactory')}
(Path.home()/'.cache/hackathon/show-harness-train-venv/training-ready.json').write_text(json.dumps(versions,indent=2))
print(json.dumps(versions))
