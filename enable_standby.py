"""Explicitly enable both Piper arms in standby; send no motion targets."""
import datetime
import fcntl
import json
from pathlib import Path
import time

import piper_control as pc


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true')
    if not parser.parse_args().execute:
        parser.error('explicit --execute required to enable motors')
    root = Path(__file__).resolve().parent
    report = {'time': datetime.datetime.now().astimezone().isoformat(),
              'operation': 'explicit enable both arms; retain standby; no joint targets',
              'arms': {}, 'ok': False}
    transports = {}
    active = None
    try:
        with (Path.home()/'.cache/hackathon/piper-control.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            for can in ('can0','can1'):
                pc.verify_control_device(can)
                receiver = pc.Receiver(can)
                sdk = pc.sdk_connect(can)
                transports[can] = receiver,sdk
                receiver.collect(.35)
                f = receiver.feedback
                f.healthy(time.monotonic())
                if f.frames[0x2A1][0][0] != 0:
                    raise RuntimeError(can+': standby required; no mode transition')
                pc.check_mode_transition(f)
                pc.query_limits(sdk,receiver)
                origin = f.joints()
                receiver.collect(.2)
                f.healthy(time.monotonic())
                if max(abs(a-b) for a,b in zip(origin,f.joints()))>100:
                    raise RuntimeError(can+': arm not stationary')
                outside = [i for i,q in enumerate(origin,1)
                           if not f.limits[i][0]<=q<=f.limits[i][1]]
                report['arms'][can] = {'before':f.snapshot(),'origin_mdeg':origin,
                                       'outside_limit_joints':outside,'enable_sent':False}
            for can,(receiver,sdk) in transports.items():
                record = report['arms'][can]
                origin = record['origin_mdeg']
                receiver.collect(.15)
                f = receiver.feedback
                f.healthy(time.monotonic())
                pc.check_mode_transition(f)
                if f.frames[0x2A1][0][0]!=0 or max(abs(a-b) for a,b in zip(origin,f.joints()))>100:
                    raise RuntimeError(can+': standby/pose changed after planning')
                active = sdk
                sdk.EnableArm(7)
                record['enable_sent'] = True
                deadline = time.monotonic()+3
                stable_since = None
                peak = 0
                while time.monotonic()<deadline:
                    receiver.collect(.05)
                    f.healthy(time.monotonic())
                    if f.frames[0x2A1][0][0]!=0:
                        raise RuntimeError(can+': mode unexpectedly changed')
                    pc.check_mode_transition(f)
                    peak=max(peak,max(abs(a-b) for a,b in zip(origin,f.joints())))
                    if peak>500:
                        raise RuntimeError(can+': unexpected motion over 0.5 degree')
                    if all(f.snapshot()['enabled']):
                        if stable_since is None: stable_since=time.monotonic()
                        if time.monotonic()-stable_since>=.5: break
                    else: stable_since=None
                else: raise RuntimeError(can+': enable not acknowledged continuously')
                f.healthy(time.monotonic(),enabled=True)
                record.update(ok=True,after=f.snapshot(),max_change_deg=peak/1000)
                active = None
            for can,(receiver,sdk) in transports.items():
                receiver.collect(.1)
                f=receiver.feedback
                f.healthy(time.monotonic(),enabled=True)
                if f.frames[0x2A1][0][0]!=0:raise RuntimeError(can+': final mode changed')
                report['arms'][can]['final']=f.snapshot()
            report['ok']=True
    except (Exception,KeyboardInterrupt) as exc:
        report['error']=str(exc) or 'interrupted'
        if active:
            active.MotionCtrl_1(1,0,0)
            report['emergency_stop_sent']=True
    finally:
        for receiver,sdk in transports.values():
            receiver.socket.close();sdk.DisconnectPort()
        path=root/'results/show_harness/enable'/datetime.datetime.now().strftime('%Y%m%d-%H%M%S')/'report.json'
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(report,indent=2)+'\n')
        summary={'report':str(path),'ok':report['ok'],'error':report.get('error'),
                 'arms':{can:{k:r.get(k) for k in ('ok','enable_sent','max_change_deg','outside_limit_joints')}
                         | {'enabled':r.get('final',r.get('after',{})).get('enabled'),
                            'mode':r.get('final',r.get('after',{})).get('ctrl_mode'),
                            'fault_code':r.get('final',r.get('after',{})).get('fault_code')}
                         for can,r in report['arms'].items()}}
        print(json.dumps(summary,indent=2))
    return 0 if report['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
