"""Explicit supported-pose recovery of enabled, stationary arms into queried limits."""
import argparse
import datetime
import fcntl
import json
from pathlib import Path
import time

import piper_control as pc
from supported_restore import plan_supported_restore


def recover(receiver, sdk, target, origin):
    f = receiver.feedback
    f.healthy(time.monotonic(), enabled=True)
    f.validate_target(target)
    # Prepare the valid target while still in standby, then enter at zero speed.
    sdk.JointCtrl(*target)
    sdk.MotionCtrl_2(1, 1, 0)
    receiver.collect(.15)
    f.healthy(time.monotonic(), enabled=True)
    if f.frames[0x2A1][0][0] != 1:
        raise RuntimeError('CAN mode transition rejected; no Reset fallback')
    peak = [0] * 6
    deadline, stable = time.monotonic() + 15, 0
    while time.monotonic() < deadline:
        sdk.JointCtrl(*target)
        sdk.MotionCtrl_2(1, 1, 5)
        receiver.collect(.025)
        f.healthy(time.monotonic(), enabled=True)
        if f.frames[0x2A1][0][0] != 1:
            raise RuntimeError('CAN control lost')
        joints = f.joints()
        peak = [max(p, abs(q-o)) for p, q, o in zip(peak, joints, origin)]
        if max(peak) > 9000:
            raise RuntimeError('recovery exceeded 9 degree movement budget')
        # Must remain in the corridor between the resting pose and the valid goal.
        if any(q < min(o,t)-1000 or q > max(o,t)+1000 for q,o,t in zip(joints,origin,target)):
            raise RuntimeError('joint moved outside recovery corridor')
        stable = stable + 1 if max(abs(a-b) for a,b in zip(joints,target)) <= 200 else 0
        if stable >= 20:
            f.validate_target(joints)
            return {'after': f.snapshot(), 'max_change_deg': [p/1000 for p in peak]}
    raise RuntimeError('recovery target timeout')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--supported', action='store_true')
    args = parser.parse_args()
    if args.execute and not args.supported:
        parser.error('execution requires operator-confirmed supported resting arms')
    report = {'time': datetime.datetime.now().astimezone().isoformat(),
              'operation': 'enabled resting-pose recovery into original hardware limits',
              'execute': args.execute, 'limit_changes': False, 'arms': {}, 'ok': False}
    transports = {}
    active = None
    try:
        with (Path.home()/'.cache/hackathon/piper-control.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Prepare and validate BOTH arms before moving either one.
            for can in ('can0', 'can1'):
                pc.verify_control_device(can)
                receiver = pc.Receiver(can)
                sdk = pc.sdk_connect(can)
                transports[can] = (receiver, sdk)
                receiver.collect(.4)
                f = receiver.feedback
                f.healthy(time.monotonic(), enabled=True)
                if f.frames[0x2A1][0][0] != 0:
                    raise RuntimeError(can + ': already-enabled standby required')
                if any(cid in f.counts for cid in range(0x150,0x160)):
                    raise RuntimeError(can + ': another motion controller present')
                pc.query_limits(sdk, receiver)
                origin = f.joints()
                target = plan_supported_restore(origin, f.limits)
                f.validate_target(target)
                receiver.collect(.2)
                f.healthy(time.monotonic(), enabled=True)
                if max(abs(a-b) for a,b in zip(f.joints(),origin)) > 100:
                    raise RuntimeError(can + ': arm is moving/being dragged')
                report['arms'][can] = {'before': f.snapshot(), 'origin_mdeg': origin,
                    'target_mdeg': target, 'speed_percent': 5, 'executed': False}
            if args.execute:
                for can, (receiver,sdk) in transports.items():
                    record = report['arms'][can]
                    receiver.collect(.1)
                    receiver.feedback.healthy(time.monotonic(), enabled=True)
                    if max(abs(a-b) for a,b in zip(receiver.feedback.joints(),record['origin_mdeg'])) > 100:
                        raise RuntimeError(can + ': pose changed after planning')
                    active = sdk
                    record['executed'] = True
                    record.update(recover(receiver,sdk,record['target_mdeg'],record['origin_mdeg']))
                    active = None
            report['ok'] = True
    except (Exception, KeyboardInterrupt) as exc:
        report['error'] = str(exc) or 'interrupted'
        if active:
            active.MotionCtrl_1(1,0,0)
            report['emergency_stop_sent'] = True
    finally:
        for receiver,sdk in transports.values():
            receiver.socket.close()
            sdk.DisconnectPort()
        path = Path(__file__).resolve().parent/'results/show_harness/resting-recovery'/datetime.datetime.now().strftime('%Y%m%d-%H%M%S')/'report.json'
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({'report': str(path), **report},indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
