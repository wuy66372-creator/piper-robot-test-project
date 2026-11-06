"""One isolated dataset conversion + LLaMA-Factory LoRA job."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'Show-Harness')]
import yaml
from collection import write_json
from dataset_store import prepare_training
from training_manager import CACHE,TRAIN_ENV,base_path


def main():
    path=Path(sys.argv[1]).resolve()
    job=json.loads((path/'job.json').read_text())
    def interrupted(*_):raise InterruptedError('Training cancelled')
    signal.signal(signal.SIGTERM,interrupted)
    process=None
    try:
        options=job['options']
        count=prepare_training(path/'episodes',options['mode'],path/'dataset',grasp_repeat=options.get('grasp_repeat',5))
        sampling=json.loads((path/'dataset/sampling.json').read_text())
        job['sampling']=sampling
        print('GRASP sampling: '+json.dumps(sampling,ensure_ascii=False),flush=True)
        config={
            'model_name_or_path':str(base_path(options['profile'])),
            'image_max_pixels':65536,'image_min_pixels':65536,'trust_remote_code':False,
            'flash_attn':'sdpa','enable_liger_kernel':False,
            'stage':'sft','do_train':True,'finetuning_type':'lora',
            'freeze_vision_tower':True,'freeze_multi_modal_projector':True,
            'lora_target':'q_proj,v_proj','lora_rank':options['rank'],
            'lora_alpha':options['rank']*2,'lora_dropout':.05,
            'dataset':'gumi','dataset_dir':str(path/'dataset'),'template':'qwen3_5_nothink',
            'cutoff_len':2048,'overwrite_cache':True,'preprocessing_num_workers':1,
            'dataloader_num_workers':0,'per_device_train_batch_size':1,
            'gradient_accumulation_steps':4,'learning_rate':options['learning_rate'],
            'num_train_epochs':options['epochs'],'max_steps':options['max_steps'],
            'lr_scheduler_type':'cosine','warmup_ratio':.05,'bf16':True,
            'disable_gradient_checkpointing':False,
            'output_dir':str(path/'adapter'),'logging_steps':1,'save_steps':50,
            'save_total_limit':2,'plot_loss':True,'overwrite_output_dir':False,
            'report_to':'none','seed':42,
        }
        from training_initialization import training_config
        config.update(training_config(options,path))
        if options['quantization']:
            config.update(quantization_bit=4,quantization_method='bitsandbytes',double_quantization=True)
        (path/'train.yaml').write_text(yaml.safe_dump(config,sort_keys=False))
        job.update(samples=count,status='starting',contract={'mode':options['mode'],
                   'step_m':.02,'template':'qwen3_5_nothink','execution_token_swap':job['execution_token_swap'],
                   'gripper_state':'width-last-command-v1'})
        write_json(path/'job.json',job)
        # Stop only the existing managed inference process; model_service refuses foreign PIDs.
        subprocess.run(['bash',str(ROOT/'show_harness/models.sh'),'stop'],check=True,timeout=35)
        if (path/'cancel.request').exists():raise InterruptedError('Training cancelled')
        env=dict(os.environ,CUDA_VISIBLE_DEVICES='0',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',
                 WANDB_DISABLED='true',TOKENIZERS_PARALLELISM='false',PYTHONUNBUFFERED='1',
                 PYTHONPATH=str(CACHE/'llamafactory-9ce6b663/src'))
        job['status']='training';write_json(path/'job.json',job)
        process=subprocess.Popen([str(TRAIN_ENV/'bin/llamafactory-cli'),'train',str(path/'train.yaml')],env=env)
        code=process.wait()
        if (path/'cancel.request').exists():raise InterruptedError('Training cancelled')
        if code:raise RuntimeError(f'LlamaFactory exited {code}; inspect train.log')
        if not (path/'adapter/adapter_model.safetensors').is_file():raise RuntimeError('Trainer exited without a LoRA adapter')
        write_json(path/'adapter/gumi_contract.json',job['contract'])
        write_json(path/'adapter/gumi_initialization.json',job.get('initialization',{'type':'base'}))
        job['status']='completed'
    except (Exception,KeyboardInterrupt) as exc:
        if process and process.poll() is None:
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait()
        job.update(status='cancelled' if isinstance(exc,InterruptedError) or (path/'cancel.request').exists() else 'failed',error=str(exc))
        print(str(exc),flush=True)
    finally:
        write_json(path/'job.json',job)


if __name__=='__main__':main()
