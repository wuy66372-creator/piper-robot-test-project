"""One-step infrastructure test. Synthetic fixtures NEVER enter the human data catalog."""
import json
import argparse
import os
import signal
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'Show-Harness')]
import numpy as np
from collection import DemonstrationRecorder,write_json


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--dual-serving',action='store_true',help='Train v4 and verify real local three-image serving, without robot access')
    parser.add_argument('--upstream',action='store_true',help='Verify actual continuation of the pinned upstream adapter')
    args=parser.parse_args()
    if args.upstream and args.dual_serving:parser.error('Installed upstream adapters are single-arm v3')
    from model_service import PIDFILE
    restore_profile=json.loads(PIDFILE.read_text())['profile'] if PIDFILE.is_file() else 'qwen3_5_4b'
    root=ROOT/'results/verification/lora-smoke'/time.strftime('%Y%m%d-%H%M%S')
    root.mkdir(parents=True)
    recorder=DemonstrationRecorder(root/'episodes')
    recorder.session_meta={'task':'synthetic infrastructure smoke test','task_text':'synthetic infrastructure smoke test',
                           'source':'synthetic_smoke_test','synthetic':True}
    recorder.start()
    image=np.zeros((256,256,3),dtype=np.uint8)
    image[64:192,64:192]=[180,90,40]
    obs={'agentview':image,'wrist_left':image,'wrist_right':image,'front_camera_available':True,
         'left':{'ee_pose':np.array([0.,0.,.3,0.,0.,0.,1.]),'gripper_width':.07},
         'right':{'ee_pose':np.array([0.,0.,.3,0.,0.,0.,1.]),'gripper_width':.07}}
    recorder.add_step({'left':'MV_UP','right':'STILL'},{'left':'move','right':'still'},obs,{'left':False,'right':False})
    recorder.finish_step({'ok':True},{'left':.02})
    recorder.terminal(obs)
    recorder.session_meta.update(success=True,status='saved')
    recorder.stop()
    job=dict(id='isolated_synthetic_smoke',status='preparing',synthetic=True,
             options=dict(profile='qwen3_5_0_8b',mode='dual_v4' if args.dual_serving else 'left_v3',epochs=1.,max_steps=2 if args.upstream else 1,
                          rank=64 if args.upstream else 4,learning_rate=2e-5 if args.upstream else 1e-4,quantization=0,
                          initialization='upstream_lora' if args.upstream else 'base'),
             execution_token_swap=[] if args.dual_serving else ['MV_FWD','MV_BACK'])
    write_json(root/'job.json',job)
    if args.upstream:
        from training_initialization import snapshot
        job['initialization']=snapshot('qwen3_5_0_8b',root/'source_adapter')
        write_json(root/'job.json',job)
    print('SMOKE_OUTPUT='+str(root),flush=True)
    try:
        code=subprocess.call([sys.executable,'-B',str(Path(__file__).with_name('training_worker.py')),str(root)])
        result=json.loads((root/'job.json').read_text())
        print(json.dumps(result,indent=2))
        if args.upstream and result['status']=='completed':
            import hashlib
            from training_initialization import upstream
            original,_=upstream('qwen3_5_0_8b')
            def digest(path):
                with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
            expected=job['initialization']['sha256']['adapter_model.safetensors']
            assert digest(original/'adapter_model.safetensors')==expected
            assert digest(root/'source_adapter/adapter_model.safetensors')==expected
            assert digest(root/'adapter/adapter_model.safetensors')!=expected
            config=json.loads((root/'adapter/adapter_config.json').read_text())
            assert config['r']==64
            print('UPSTREAM_CONTINUATION_OK: rank64; trained adapter changed; source and snapshot unchanged',flush=True)
        if args.dual_serving and result['status']=='completed':
            serving=subprocess.Popen([str(Path.home()/'.cache/hackathon/show-harness-vllm-venv/bin/python'),'-B',
                str(Path(__file__).with_name('smoke_serve_adapter.py')),str(root)],start_new_session=True)
            try:
                import requests
                from core.record.images import image_to_data_url
                deadline=time.monotonic()+240
                while time.monotonic()<deadline:
                    if serving.poll() is not None:raise RuntimeError('Verification server exited')
                    try:
                        if any(m['id']=='verification_v4_showharness_ft' for m in requests.get('http://127.0.0.1:8000/v1/models',timeout=1).json()['data']):break
                    except (requests.RequestException,ValueError,KeyError):pass
                    time.sleep(2)
                else:raise RuntimeError('Verification model load timed out')
                prompt=(ROOT/'Show-Harness/prompts/v4/dual_mvtoken_once.txt').read_text().format(task='synthetic test',recent_left='none',recent_right='none')
                content=[{'type':'image_url','image_url':{'url':image_to_data_url(image)}} for _ in range(3)]+[{'type':'text','text':prompt}]
                response=requests.post('http://127.0.0.1:8000/v1/chat/completions',json=dict(model='verification_v4_showharness_ft',
                    messages=[{'role':'user','content':content}],max_tokens=24,temperature=0,chat_template_kwargs={'enable_thinking':False}),timeout=90)
                response.raise_for_status();write_json(root/'three-view-serving.json',response.json())
                print('THREE_VIEW_CUSTOM_LORA_SERVING_OK: '+response.json()['choices'][0]['message']['content'],flush=True)
            finally:
                if serving.poll() is None:
                    os.killpg(serving.pid,signal.SIGTERM)
                    try:serving.wait(timeout=30)
                    except subprocess.TimeoutExpired:os.killpg(serving.pid,signal.SIGKILL);serving.wait()
        return 0 if code==0 and result['status']=='completed' else 1
    finally:
        subprocess.run(['bash',str(ROOT/'show_harness/models.sh'),'start',restore_profile],timeout=35)


if __name__=='__main__':raise SystemExit(main())
