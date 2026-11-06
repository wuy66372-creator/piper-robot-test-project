"""Isolated serving fixture; never registers synthetic jobs in the production catalog."""
import json
from pathlib import Path
import sys
import serve_models

root=Path(sys.argv[1]).resolve()
if not root.is_relative_to(serve_models.ROOT/'results/verification/lora-smoke'):raise SystemExit('Verification directory required')
job=json.loads((root/'job.json').read_text())
if not job.get('synthetic') or job['status']!='completed' or job['options']['profile']!='qwen3_5_0_8b' or job['options']['mode']!='dual_v4':
    raise SystemExit('Completed synthetic 0.8B dual fixture required')
# Inject an isolated fixture into the same command builder used by production.
# The HTTP API still resolves ONLY real completed jobs; this child has no robot API.
serve_models.resolve=lambda _:dict(base='qwen3_5_0_8b',arm='dual',model='verification_v4_showharness_ft',adapter=str(root/'adapter'))
sys.argv=['serve_models.py','verification_v4']
serve_models.main()
