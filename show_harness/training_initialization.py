"""Pinned upstream adapter validation and immutable continuation snapshots."""
import hashlib
import json
from pathlib import Path
import shutil
from model_profiles import STORE,ROOT,BASES

TARGETS={'q_proj','k_proj','v_proj','o_proj','in_proj_qkv','in_proj_a','in_proj_b','in_proj_z',
         'out_proj','gate_proj','up_proj','down_proj'}

def upstream(profile):
    if profile not in BASES:raise ValueError('Unsupported upstream base')
    path=STORE/'adapters'/profile
    if path.is_symlink():raise ValueError('Unsafe upstream adapter path')
    for name in ('adapter_config.json','adapter_model.safetensors'):
        file=path/name
        if file.is_symlink() or not file.is_file():raise ValueError('Upstream LoRA not installed: '+profile)
    config=json.loads((path/'adapter_config.json').read_text())
    if (config.get('base_model_name_or_path')!=BASES[profile] or config.get('peft_type')!='LORA'
            or config.get('r')!=64 or set(config.get('target_modules',[]))!=TARGETS or config.get('modules_to_save')):
        raise ValueError('Unexpected upstream LoRA configuration')
    return path,config

def available():
    result=[]
    for profile in BASES:
        try:
            _,config=upstream(profile)
            result.append(dict(profile=profile,rank=config['r'],modes=['left_v3','right_v3']))
        except (ValueError,OSError,TypeError):continue
    return result

def snapshot(profile,destination):
    path,config=upstream(profile)
    lock=json.loads((ROOT/'show_harness/models.lock.json').read_text())
    receipt=json.loads((STORE/'download_receipt.json').read_text())
    entry=next((r for r in receipt['downloads'] if r['repo']==lock['adapter_repo'] and r['revision']==lock['adapter_revision']),None)
    if entry is None:raise ValueError('Pinned upstream download receipt is missing')
    expected=next((f for f in entry['files'] if f['file']==profile+'/adapter_model.safetensors'),None)
    if not expected or not expected.get('sha256'):raise ValueError('Verified upstream weight hash is missing')
    destination=Path(destination);destination.mkdir()
    hashes={}
    for name in ('adapter_config.json','adapter_model.safetensors'):
        target=destination/name;shutil.copy2(path/name,target)
        with target.open('rb') as f:hashes[name]=hashlib.file_digest(f,'sha256').hexdigest()
    if hashes['adapter_model.safetensors']!=expected['sha256']:raise ValueError('Upstream LoRA hash mismatch')
    if json.loads((destination/'adapter_config.json').read_text())!=config:raise ValueError('Upstream adapter changed during snapshot')
    return dict(type='upstream_lora',profile=profile,repo=lock['adapter_repo'],revision=lock['adapter_revision'],
                rank=config['r'],target_modules=config['target_modules'],sha256=hashes)

def training_config(options,job_path):
    """Resume the saved adapter itself; never merge it then create a random LoRA."""
    if options.get('initialization','base')=='base':return {}
    path=Path(job_path)/'source_adapter'
    config=json.loads((path/'adapter_config.json').read_text())
    return dict(adapter_name_or_path=str(path),create_new_adapter=False,lora_rank=config['r'],
                lora_alpha=config['lora_alpha'],lora_dropout=config.get('lora_dropout',.05),
                lora_target=','.join(config['target_modules']))
