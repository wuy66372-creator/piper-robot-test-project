"""Resolve fixed released profiles or completed, confined GUMI training jobs."""
import json
import os
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
STORE=Path(os.environ.get('SHOWHARNESS_MODEL_HOME',Path.home()/'.cache/hackathon/show-harness-models'))
JOBS=ROOT/'data/lora_jobs'
BASES={'qwen3_5_0_8b':'Qwen/Qwen3.5-0.8B','qwen3_5_2b':'Qwen/Qwen3.5-2B','qwen3_5_4b':'Qwen/Qwen3.5-4B'}
JOB_ID=re.compile(r'job_[0-9]{8}_[0-9]{6}_[0-9a-f]{8}')

def resolve(profile,arm=None):
    if not isinstance(profile,str):raise ValueError('请选择模型或已完成的LoRA作业')
    if profile in BASES:
        return dict(id=profile,base=profile,model=profile+'_showharness_ft',name=BASES[profile]+' · 上游LoRA',arm=None)
    if not JOB_ID.fullmatch(profile):raise ValueError('Unsupported real-arm model profile')
    root=JOBS.resolve();job_dir=JOBS/profile
    if job_dir.is_symlink() or job_dir.resolve().parent!=root:raise ValueError('Invalid job path')
    adapter=job_dir/'adapter'
    if adapter.is_symlink() or adapter.resolve().parent!=job_dir.resolve():raise ValueError('Invalid adapter path')
    def read(name,parent):
        path=parent/name
        if path.is_symlink() or not path.is_file():raise ValueError('Missing or unsafe '+name)
        return json.loads(path.read_text())
    job=read('job.json',job_dir)
    if job.get('status')!='completed' or job.get('synthetic'):raise ValueError('仅能加载已完成的正式训练作业')
    options=job['options'];base=options['profile'];mode=options['mode']
    if base not in BASES:raise ValueError('Unsupported training base')
    if mode not in ('left_v3','right_v3','dual_v4'):raise ValueError('Unsupported prompt contract')
    side='dual' if mode=='dual_v4' else mode.split('_')[0]
    if arm is not None and arm!=side:raise ValueError('此LoRA仅适用于'+side+'臂')
    contract=read('gumi_contract.json',adapter)
    from gripper_context import CONTRACT
    if contract.get('gripper_state') not in (None,CONTRACT):raise ValueError('Unsupported gripper state input contract')
    if (contract.get('mode')!=mode or contract.get('step_m')!=.02 or
            contract.get('template')!='qwen3_5_nothink' or contract.get('execution_token_swap')!=([] if side=='dual' else ['MV_FWD','MV_BACK'])):
        raise ValueError('LoRA步长、提示格式或方向映射不匹配')
    config=read('adapter_config.json',adapter)
    base_path=STORE/'huggingface/hub'/BASES[base]
    if config.get('base_model_name_or_path') not in (BASES[base],str(base_path),str(base_path.resolve())):
        raise ValueError('LoRA基座与训练作业不匹配')
    modules=config.get('target_modules',[])
    continued=options.get('initialization')=='upstream_lora'
    allowed={'q_proj','v_proj'}
    if continued:
        from training_initialization import TARGETS
        allowed=TARGETS
        if mode=='dual_v4' or config.get('r')!=64 or job.get('initialization',{}).get('type')!='upstream_lora':
            raise ValueError('Invalid upstream continuation contract')
    if (not isinstance(modules,list) or not modules or
            any(not isinstance(m,str) or not re.fullmatch(r'(?:model\.language_model\.layers\.\d+\.(?:self_attn|linear_attn|mlp)\.)?[a-z_]+',m) for m in modules)):
        raise ValueError('Unsupported LoRA target modules')
    if (config.get('peft_type')!='LORA' or type(config.get('r')) is not int or config['r'] not in (4,8,16,32,64)
            or {m.rsplit('.',1)[-1] for m in modules}!=allowed or config.get('modules_to_save')):
        raise ValueError('Unsupported LoRA configuration')
    weights=adapter/'adapter_model.safetensors'
    if weights.is_symlink() or not weights.is_file() or not weights.stat().st_size:raise ValueError('Missing LoRA weights')
    return dict(id=profile,base=base,model=profile+'_showharness_ft',adapter=str(adapter),arm=side,
                gripper_state=contract.get('gripper_state'),
                name=BASES[base]+' · 自训 '+side+' · '+profile)

def catalog():
    result=[resolve(p) for p in BASES]
    for path in sorted(JOBS.glob('job_*/job.json'),reverse=True):
        try:result.append(resolve(path.parent.name))
        except (ValueError,OSError,KeyError,TypeError):continue
    return result

def config_for(settings,profile,arm=None):
    from core.config import resolve_vlm_config
    spec=resolve(profile,arm)
    config=resolve_vlm_config(settings,spec['base'])
    config['gripper_state']=spec.get('gripper_state')
    if spec.get('adapter'):
        config['model']=spec['model']
        if spec['arm']=='dual':config['execution_token_swap']=[]
    return config
