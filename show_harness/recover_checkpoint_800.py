"""Recover the explicitly requested job checkpoint without loading a robot/model."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import shutil
import torch
from safetensors import safe_open
from model_profiles import ROOT,resolve
from training_initialization import TARGETS

JOB='job_20261022_115213_2e8bfd91'
STEP=800

def digest(path):
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def write_json(path,data):
    temporary=path.with_name(path.name+'.recovery.tmp')
    with temporary.open('x') as f:json.dump(data,f,ensure_ascii=False,indent=2);f.write('\n')
    temporary.replace(path)

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--execute',action='store_true');args=parser.parse_args()
    root=ROOT/'data/lora_jobs'/JOB;adapter=root/'adapter';checkpoint=adapter/f'checkpoint-{STEP}'
    for path in (root,adapter,checkpoint):
        if path.is_symlink() or not path.is_dir():raise ValueError('Unsafe/missing recovery path')
    job=json.loads((root/'job.json').read_text());state=json.loads((checkpoint/'trainer_state.json').read_text())
    config=json.loads((checkpoint/'adapter_config.json').read_text())
    if job['status'] not in ('cancelled','interrupted','failed'):raise ValueError('Job is not interrupted/cancelled')
    if state['global_step']!=STEP:raise ValueError('Checkpoint step mismatch')
    if job['options']['profile']!='qwen3_5_4b' or job['options']['mode']!='left_v3':raise ValueError('Unexpected training contract')
    if config['base_model_name_or_path']!='Qwen/Qwen3.5-4B' or config['r']!=64 or set(config['target_modules'])!=TARGETS:
        raise ValueError('Adapter/base/rank mismatch')
    if job['contract']!={'mode':'left_v3','step_m':.02,'template':'qwen3_5_nothink','execution_token_swap':['MV_FWD','MV_BACK']}:
        raise ValueError('Unexpected action contract')
    weights=checkpoint/'adapter_model.safetensors'
    if weights.is_symlink():raise ValueError('Symlink checkpoint weights refused')
    count=0
    with safe_open(weights,framework='pt',device='cpu') as tensors:
        for name in tensors.keys():
            if not name.startswith('base_model.model.model.language_model.layers.') or not any(k in name for k in ('.lora_A.','.lora_B.')):
                raise ValueError('Unexpected tensor: '+name)
            tensor=tensors.get_tensor(name)
            if tensor.ndim!=2 or not torch.isfinite(tensor).all():raise ValueError('Invalid tensor: '+name)
            count+=1
    if count==0:raise ValueError('Empty checkpoint')
    sha=digest(weights)
    print(json.dumps(dict(valid=True,job=JOB,step=STEP,target_steps=state['max_steps'],epoch=state['epoch'],tensors=count,sha256=sha)),flush=True)
    if not args.execute:return
    backup=root/f'job.before-checkpoint-{STEP}.json'
    with backup.open('x') as f:f.write((root/'job.json').read_text())
    files=['adapter_model.safetensors','adapter_config.json','trainer_state.json','README.md',
           'chat_template.jinja','processor_config.json','tokenizer.json','tokenizer_config.json']
    for name in files:
        source=checkpoint/name;target=adapter/name
        if not source.is_file() or source.is_symlink() or target.exists():raise ValueError('Missing source or existing destination: '+name)
    progress=adapter/'trainer_log.jsonl';progress_backup=adapter/f'trainer_log.before-checkpoint-{STEP}.jsonl'
    if progress_backup.exists():raise ValueError('Recovery backup already exists')
    shutil.copy2(progress,progress_backup)
    for name in files:
        temporary=adapter/(name+'.recovery.tmp');shutil.copy2(checkpoint/name,temporary);temporary.replace(adapter/name)
    if digest(adapter/'adapter_model.safetensors')!=sha:raise ValueError('Copied checkpoint hash mismatch')
    write_json(adapter/'gumi_contract.json',job['contract'])
    write_json(adapter/'gumi_initialization.json',job['initialization'])
    recovery=dict(checkpoint=f'checkpoint-{STEP}',step=STEP,epoch=state['epoch'],original_target_steps=state['max_steps'],
        previous_status=job['status'],previous_error=job.get('error'),sha256=sha,
        recovered_at=datetime.datetime.now().astimezone().isoformat(),note='User accepted checkpoint-800; original 834-step training was not completed.')
    write_json(adapter/'gumi_recovery.json',recovery)
    lines=[]
    for line in progress_backup.read_text().splitlines():
        item=json.loads(line)
        if item.get('current_steps',STEP+1)<=STEP:lines.append(line)
    temporary=progress.with_name(progress.name+'.recovery.tmp')
    with temporary.open('x') as f:f.write('\n'.join(lines)+'\n')
    temporary.replace(progress)
    job.update(status='completed',recovered_from_checkpoint=recovery,completion_note='Accepted checkpoint-800 (800/834 steps)')
    job.pop('error',None);write_json(root/'job.json',job)
    try:spec=resolve(JOB,'left')
    except Exception:
        write_json(root/'job.json',json.loads(backup.read_text()));raise
    print(json.dumps(dict(ok=True,profile=spec['id'],adapter=spec['adapter'],checkpoint_preserved=checkpoint.is_dir(),backup=str(backup))),flush=True)

if __name__=='__main__':main()
