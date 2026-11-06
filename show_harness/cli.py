"""Local Show-Harness verification and opt-in bounded joint commissioning."""
from contextlib import ExitStack
from pathlib import Path
import argparse
import json
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'Show-Harness'), str(Path(__file__).parent)]

import cv2
import numpy as np
from scipy.spatial.transform import Rotation
from local_backend import LocalPiperInterface, LocalRGBCamera, install, target_units
from core.piper.piper_session import PiperSession, PiperSessionConfig
from core.piper.kinematics import select_dh_variant
from interpreters.piper_atomic_controller import PiperAtomicController
import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['preflight','jog'])
    parser.add_argument('--arm',choices=['left','right'],default='left')
    parser.add_argument('--delta',type=float,default=1.)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    install(allow_motion=args.command=='jog' and args.execute)
    output = args.output or ROOT/'results/show_harness'/time.strftime('%Y%m%d-%H%M%S')
    if output.exists():
        raise RuntimeError('use a new output directory')
    output.mkdir(parents=True)
    report={'upstream_commit':'c3a133eb111a8100bcb64509b1ea891fdcee1532',
            'backend':'local_socketcan_orbbec_sdk','command':args.command,'executed':False,
            'front_camera_available':False,'autonomous_ready':False,'arms':{}}
    sessions={}
    with ExitStack() as stack:
        for side in ('left','right'):
            session=PiperSession(PiperSessionConfig(arm=side,connect_cameras=False,verbose=False))
            sessions[side]=session
            stack.callback(session.close)
            session.connect()
            robot=session.robot
            joints=robot.get_joint_positions()
            pose=robot.get_ee_pose()
            kin,error=select_dh_variant(joints,pose)
            fk=kin.fk_pose7(joints)
            orientation_error=Rotation.from_quat(fk[3:]).inv()*Rotation.from_quat(pose[3:])
            info={'joints_rad':joints.tolist(),'ee_pose':pose.tolist(),
                  'gripper_width_m':float(robot.get_gripper_position()[0]),
                  'joint_velocities_rad_s':robot.get_joint_velocities().tolist(),
                  'status':robot.arm_status(),'dh_variant':kin.dh_variant,
                  'fk_position_error_mm':error*1000,
                  'fk_orientation_error_deg':float(np.degrees(orientation_error.magnitude()))}
            primitives=yaml.safe_load((ROOT/'Show-Harness/configs/primitives_piper.yaml').read_text())
            controller=PiperAtomicController.from_primitives_config(
                robot,primitives,step_m=.001,motion_frame='wrist',verbose=False)
            controller.sync_from_robot()  # initializes setpoints from feedback; no command emitted
            info['upstream_joint_stream_ready']=controller._joint_stream_ok
            report['arms'][side]=info
            if args.command=='preflight':
                cam=LocalRGBCamera(f'/camera_{"l" if side=="left" else "r"}/color/image_raw')
                stack.callback(cam.release)
                info['_camera']=cam
        if args.command=='preflight':
            start=time.monotonic()
            counts={'left':0,'right':0}
            indices={'left':set(),'right':set()}
            while time.monotonic()-start<3:
                for side,info in report['arms'].items():
                    ok,frame,_=info['_camera'].read()
                    if not ok:
                        raise RuntimeError(f'{side}: missing/stale wrist frame')
                    counts[side]+=1
                    meta=json.loads((info['_camera'].directory/(info['_camera'].serial+'.json')).read_text())
                    indices[side].add(meta['index'])
                    info['wrist_shape']=list(frame.shape)
                    cv2.imwrite(str(output/f'{side}_wrist.jpg'),cv2.cvtColor(frame,cv2.COLOR_RGB2BGR))
                time.sleep(.1)
            for side,info in report['arms'].items():
                info.pop('_camera')
                info['fresh_camera_reads']=counts[side]
                info['unique_wrist_frames']=len(indices[side])
                if len(indices[side])<20:
                    raise RuntimeError(f'{side}: camera is not advancing')
        for info in report['arms'].values():
            if info['fk_position_error_mm']>10 or info['fk_orientation_error_deg']>2:
                raise RuntimeError('FK does not match device end-pose feedback')
        if args.command=='jog':
            side=args.arm
            robot=sessions[side].robot
            info=report['arms'][side]
            joints=robot.get_joint_positions()
            if not np.isfinite(args.delta) or abs(args.delta)>2:
                raise ValueError('jog requires finite delta within 2 degrees')
            target=joints.copy();target[5]+=np.radians(args.delta)
            with robot.mutex:
                target_units(target,robot.origin,robot._feedback())
            info['jog_target_rad']=target.tolist()
            if args.execute:
                try:
                    robot.move_to_joint_positions(target,3.)
                    info['at_target_rad']=robot.get_joint_positions().tolist()
                finally:
                    # Return only through the same fresh-feedback and bounded command checks.
                    robot.move_to_joint_positions(joints,3.)
                info['after_return_rad']=robot.get_joint_positions().tolist()
                report['executed']=True
        for side,session in sessions.items():
            report['arms'][side]['final_status']=session.robot.arm_status()
        report['ok']=True
    (output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))

if __name__=='__main__':
    main()
