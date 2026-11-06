#!/usr/bin/env python3
import datetime,fcntl,json,pathlib,time,sys
from piper_control import Receiver,sdk_connect,verify_control_device,query_limits

def plan_supported_restore(origin, limits):
    if len(origin) != 6 or set(limits) != set(range(1, 7)):
        raise RuntimeError('six distinct limits and joint positions required')
    if any(hi - lo <= 1000 or speed <= 0 for lo, hi, speed in limits.values()):
        raise RuntimeError('invalid hardware limits')
    target = [min(max(q, limits[i][0] + 500), limits[i][1] - 500)
              for i, q in enumerate(origin, 1)]
    if max(abs(a - b) for a, b in zip(origin, target)) > 8000:
        raise RuntimeError('recovery exceeds 8 degree bound')
    return target


def main():
    import argparse
    p = argparse.ArgumentParser(description='Supported Piper recovery after reset; never resets automatically.')
    p.add_argument('--can', choices=['can0', 'can1'], default='can0')
    p.add_argument('--execute', action='store_true')
    p.add_argument('--supported', action='store_true', help='operator confirms links resting on support, clear and unloaded')
    p.add_argument('--output', type=pathlib.Path, default=pathlib.Path(__file__).resolve().parent / 'results' / ('supported_restore_' + datetime.datetime.now().strftime('%Y%m%d_%H%M%S') + '.json'))
    args = p.parse_args()
    if not (args.execute and args.supported):
        p.error('requires explicit --execute --supported; motors must already be disabled in standby')
    verify_control_device(args.can)
    out={'checked_at':datetime.datetime.now().astimezone().isoformat(),'interface':args.can,'operation':'explicit user-authorized supported-arm recovery to nearest valid hardware limits','ok':False,'executed':False}
    s=r=None;active=False
    try:
     with (pathlib.Path.home()/'.cache/hackathon/piper-control.lock').open('a') as lock:
      fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
      r=Receiver(args.can);s=sdk_connect(args.can);r.collect(.4);f=r.feedback;f.healthy(time.monotonic());query_limits(s,r)
      if f.frames[0x2A1][0][0]!=0 or any(f.snapshot()['enabled']):raise RuntimeError('expected standby and all motors disabled after supported reset')
      if any(cid in f.counts for cid in range(0x150,0x160)):raise RuntimeError('another motion controller present')
      origin=f.joints();target=plan_supported_restore(origin,f.limits)
      f.validate_target(target)
      if max(abs(a-b) for a,b in zip(origin,target))>8000:raise RuntimeError('recovery exceeds 8 degree bound')
      out.update(before=f.snapshot(),origin_deg=[q/1000 for q in origin],target_deg=[q/1000 for q in target])
      active=True;out['executed']=True
      s.MotionCtrl_2(1,1,5);s.JointCtrl(*target);r.collect(.15);f.healthy(time.monotonic())
      if f.frames[0x2A1][0][0]!=1:raise RuntimeError('CAN mode not acknowledged')
      s.EnableArm(7)
      deadline=time.monotonic()+20;stable=0;max_excursion=[0]*6
      while time.monotonic()<deadline:
       r.collect(.02);f.healthy(time.monotonic())
       q=f.joints()
       max_excursion=[max(m,abs(a-b)) for m,a,b in zip(max_excursion,q,origin)]
       if any(m>9000 for m in max_excursion):raise RuntimeError('recovery excursion exceeded 9 degrees')
       s.MotionCtrl_2(1,1,5);s.JointCtrl(*target)
       if not all(f.snapshot()['enabled']):
        s.EnableArm(7)
        if deadline-time.monotonic()<18:raise RuntimeError('enable not acknowledged')
        continue
       f.healthy(time.monotonic(),enabled=True)
       stable=stable+1 if max(abs(a-b) for a,b in zip(q,target))<=500 else 0
       if stable>=25:break
      else:raise RuntimeError('recovery target timeout')
      f.validate_target(f.joints());out.update(ok=True,max_excursion_deg=[m/1000 for m in max_excursion],after=f.snapshot());active=False
    except (Exception,KeyboardInterrupt) as e:
     out['error']=str(e) or 'interrupted'
     if active and s:
      try:s.MotionCtrl_1(1,0,0);out['emergency_stop_sent']=True
      except Exception as e:out['stop_error']=str(e)
    finally:
     if r:out['final']=r.feedback.snapshot();r.socket.close()
     if s:s.DisconnectPort()
     args.output.parent.mkdir(parents=True,exist_ok=True)
     args.output.write_text(json.dumps(out,indent=2)+'\n')
     print(json.dumps(out,indent=2))
     return 0 if out['ok'] else 1

if __name__ == '__main__':
    sys.exit(main())
