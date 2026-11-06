"""Confined demonstration catalog and immutable training snapshots."""
import importlib.util
import json
import re
import shutil
import threading
from pathlib import Path

from collection import write_json

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data/teleop'
JOBS=ROOT/'data/lora_jobs'
ID=re.compile(r'rollout_\d{3,}')


class DatasetStore:
    def __init__(self,root=DATA):
        self.root=Path(root).resolve()
        self.root.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock()
        self.active_id=lambda:None

    def path(self,identifier):
        if not isinstance(identifier,str) or not ID.fullmatch(identifier):raise ValueError('Invalid episode ID')
        path=(self.root/identifier).resolve()
        if path.parent!=self.root or not path.is_dir():raise ValueError('Episode not found')
        return path

    def recover_interrupted(self):
        for path in self.root.glob('rollout_*/metadata.json'):
            try:
                meta=json.loads(path.read_text())
                if meta.get('status')=='recording':
                    meta.update(status='interrupted',success=False,
                                interruption_reason='Service stopped before recording was saved')
                    write_json(path,meta)
            except (OSError,ValueError):continue

    def detail(self,identifier):
        path=self.path(identifier)
        meta=json.loads((path/'metadata.json').read_text())
        actions=[]
        if (path/'actions.jsonl').is_file():
            actions=[json.loads(s) for s in (path/'actions.jsonl').read_text().splitlines() if s.strip()]
        valid=(meta.get('success') is True and meta.get('status')=='saved'
               and meta.get('source')=='human_teleop' and not meta.get('synthetic')
               and meta.get('contract')=='piper-physical-2cm' and meta.get('step_m')==.02
               and bool(actions) and all(s.get('execution_status')=='succeeded' for s in actions)
               and meta.get('terminal_observation') is True and not meta.get('archived'))
        if self.active_id()==identifier:valid=False
        return dict(meta,id=identifier,steps=len(actions),trainable=valid,actions=actions)

    def list(self):
        result=[]
        with self.lock:
            for p in sorted(self.root.glob('rollout_*'),reverse=True):
                if not ID.fullmatch(p.name) or not (p/'metadata.json').is_file():continue
                try:
                    item=self.detail(p.name);item.pop('actions',None);result.append(item)
                except (ValueError,OSError):continue
        return result

    def update(self,identifier,body):
        with self.lock:
            if self.active_id()==identifier:raise RuntimeError('Cannot edit the active recording')
            path=self.path(identifier)
            meta=json.loads((path/'metadata.json').read_text())
            if 'task' in body:
                task=body['task']
                if not isinstance(task,str) or not 1<=len(task.strip())<=1000:raise ValueError('Task must be 1–1000 characters')
                meta['task']=meta['task_text']=task.strip()
            if 'archived' in body:
                if not isinstance(body['archived'],bool):raise ValueError('archived must be boolean')
                meta['archived']=body['archived']
            write_json(path/'metadata.json',meta)
            return self.detail(identifier)

    def image(self,identifier,view,step):
        if view not in ('agentview','wrist_left','wrist_right'):raise ValueError('Invalid view')
        step=int(step)
        if not 0<=step<=100000:raise ValueError('Invalid step')
        root=self.path(identifier)
        path=(root/view/f'{step:04d}.png').resolve()
        if not path.is_relative_to(root) or not path.is_file():raise ValueError('Image not found')
        return path

    def snapshot(self,ids,destination):
        if not isinstance(ids,list) or not ids or len(ids)>500 or len(set(ids))!=len(ids):
            raise ValueError('Select 1–500 distinct episodes')
        result=[]
        with self.lock:
            for identifier in ids:
                item=self.detail(identifier)
                if not item['trainable']:raise ValueError(identifier+': only successful, complete 20 mm demonstrations may train')
                result.append(item)
            for item in result:
                source=self.path(item['id'])
                target=destination/item['id']
                target.mkdir(parents=True)
                for file in source.rglob('*'):
                    if file.is_symlink():raise ValueError('Symlink media is not accepted')
                    if file.is_file() and file.suffix in ('.png','.json','.jsonl'):
                        relative=file.relative_to(source)
                        (target/relative).parent.mkdir(parents=True,exist_ok=True)
                        shutil.copy2(file,target/relative)
        return result


def resample_grasps(samples,grasp_repeat):
    if type(grasp_repeat) is not int or not 1<=grasp_repeat<=20:raise ValueError('Invalid GRASP sampling multiplier')
    return [sample for sample in samples for _ in range(grasp_repeat if 'GRASP' in sample['output'].split() else 1)]


def prepare_training(snapshot,mode,output,upstream=None,grasp_repeat=1):
    if type(grasp_repeat) is not int or not 1<=grasp_repeat<=20:raise ValueError('Invalid GRASP sampling multiplier')
    upstream=Path(upstream or ROOT/'Show-Harness')
    spec=importlib.util.spec_from_file_location('showharness_converter',upstream/'train/data_preparation/rollouts_to_alpaca.py')
    converter=importlib.util.module_from_spec(spec);spec.loader.exec_module(converter)
    samples=[]
    for path in sorted(Path(snapshot).glob('rollout_*')):
        meta=json.loads((path/'metadata.json').read_text())
        task=meta.get('task_text') or meta['task']
        if mode=='dual_v4':
            prompt=(upstream/'prompts/v4/dual_mvtoken_once.txt').read_text()
            batch=converter.convert_dual_rollout(path,prompt,'once',task_override=task)
            terminal=[str(path/'terminal'/f'{v}.png') for v in ('agentview','wrist_left','wrist_right')]
        else:
            side='left' if mode=='left_v3' else 'right'
            if mode not in ('left_v3','right_v3'):raise ValueError('Invalid training mode')
            # Match released single-arm v3 inference: physical FWD/BACK labels are swapped
            # ONCE here; the adapter contract requires the same swap at execution.
            derived=path/('single_'+side);derived.mkdir()
            rows=[]
            for line in (path/'actions.jsonl').read_text().splitlines():
                row=json.loads(line);token=row[side]['token']
                other='right' if side=='left' else 'left'
                if token=='STILL':continue
                if row[other]['token']!='STILL':raise ValueError('Simultaneous dual-arm actions require dual v4 training')
                token={'MV_FWD':'MV_BACK','MV_BACK':'MV_FWD'}.get(token,token)
                rows.append(dict(row[side],token=token,agentview=str(path/row['agentview']),
                                 wrist=str(path/row['wrist_'+side])))
            if not rows:continue
            (derived/'actions.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
            batch=converter.convert_rollout(derived,(upstream/'prompts/v3/mvtoken_generator_lite.txt').read_text(),task_override=task)
            terminal=[str(path/'terminal/agentview.png'),str(path/'terminal'/f'wrist_{side}.png')]
        # Preserve upstream history/prompt conversion; fix DONE to the actual POST-action
        # final observation, instead of reusing the last pre-action image.
        if batch:batch[-1]['images']=terminal
        # Shared state formatting is applied to the converter's exact image-aligned rows.
        # Keep the pinned upstream untouched; do not leak the current action label.
        from gripper_context import episode_states, state, add_context
        raw_rows=[json.loads(line) for line in (path/'actions.jsonl').read_text().splitlines() if line.strip()]
        contexts,last=episode_states(raw_rows)
        by_image={str((path/row['agentview']).resolve()):context for row,context in zip(raw_rows,contexts)}
        terminal_state=meta.get('terminal_gripper_state',{})
        final={s:state(terminal_state.get(s,{}).get('gripper_width'),last[s]) for s in ('left','right')}
        for sample in batch:
            context=final if sample['output'] in ('DONE','DONE DONE') else by_image[str(Path(sample['images'][0]).resolve())]
            sample['instruction']=add_context(sample['instruction'],context if mode=='dual_v4' else context[side],mode=='dual_v4')
        samples.extend(batch)
    if not samples:raise ValueError('No matching action samples in selected episodes')
    for sample in samples:
        if sample['instruction'].count('<image>')!=len(sample['images']):raise ValueError('Image slot contract mismatch')
        for image in sample['images']:
            if not Path(image).is_file():raise ValueError('Missing training image')
    output.mkdir(parents=True,exist_ok=True)
    # Oversample only after constructing original temporal histories and pre-action
    # state. A dual pair is one training sample, even if BOTH labels are GRASP.
    from collections import Counter
    unweighted=samples
    samples=resample_grasps(unweighted,grasp_repeat)
    def counts(rows):return dict(Counter(row['output'] for row in rows))
    original_grasps=sum('GRASP' in row['output'].split() for row in unweighted)
    weighted_grasps=original_grasps*grasp_repeat
    write_json(output/'samples_unweighted.json',unweighted)
    write_json(output/'sampling.json',dict(grasp_repeat=grasp_repeat,original_samples=len(unweighted),
               training_samples=len(samples),original_grasp_samples=original_grasps,
               training_grasp_samples=weighted_grasps,original_grasp_fraction=original_grasps/len(unweighted),
               training_grasp_fraction=weighted_grasps/len(samples),
               original_labels=counts(unweighted),training_labels=counts(samples)))
    write_json(output/'samples.json',samples)
    write_json(output/'dataset_info.json',{'gumi':{'file_name':'samples.json',
               'columns':{'prompt':'instruction','query':'input','response':'output','images':'images'}}})
    return len(samples)
