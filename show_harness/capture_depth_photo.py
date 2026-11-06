"""Capture one measured D435i Z16 depth image without robot connections."""
import argparse
import importlib.metadata
import json
from pathlib import Path
import time

import cv2
import numpy as np
import pyrealsense2 as rs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial',default='347622075687')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--low-power',action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    rs.log_to_file(rs.log_severity.fatal,str(args.output/'sdk-fatal.log'))
    context = rs.context()
    device = next(d for d in context.query_devices()
                  if d.get_info(rs.camera_info.serial_number)==args.serial)
    report = {'serial': args.serial, 'firmware': device.get_info(rs.camera_info.firmware_version),
              'sdk': importlib.metadata.version('pyrealsense2'),
              'checked_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'attempts': []}
    sensor = device.first_depth_sensor()
    saved_options = {}
    if args.low_power:
        for option,value in ((rs.option.emitter_enabled,0),(rs.option.global_time_enabled,0)):
            if sensor.supports(option):
                saved_options[option]=sensor.get_option(option)
                sensor.set_option(option,value)
    profiles = []
    for p in sensor.get_stream_profiles():
        if p.stream_type()==rs.stream.depth and p.format()==rs.format.z16:
            v=p.as_video_stream_profile()
            desired=((424,240,6),(480,270,6)) if args.low_power else ((640,480,30),(848,480,30),(640,480,15))
            if (v.width(),v.height(),p.fps()) in desired:
                profiles.append(p)
    profiles.sort(key=lambda p:p.as_video_stream_profile().width() if args.low_power else (p.fps()!=30,p.as_video_stream_profile().width()!=640))
    print('Depth profiles:',[str(p) for p in profiles],flush=True)
    success=False
    for profile in profiles:
        queue=rs.frame_queue(2)
        opened=started=False
        attempt={'profile':str(profile),'method':'direct_sensor_frame_queue'}
        try:
            sensor.open(profile);opened=True
            sensor.start(queue);started=True
            depth=None
            for i in range(5):
                frame=queue.wait_for_frame(8000).as_depth_frame()
                candidate=np.asanyarray(frame.get_data()).copy()
                if np.count_nonzero(candidate)>candidate.size*.02:
                    depth=candidate
            if depth is None:
                raise RuntimeError('Received no valid nonzero depth pixels')
            scale=sensor.get_depth_scale()
            meters=depth.astype(np.float32)*scale
            valid=meters[depth>0]
            # Fixed 0.2–2.0 m range; zero depth is black (unmeasured), not a surface.
            intensity=np.round(255*(1-np.clip((meters-.2)/1.8,0,1))).astype(np.uint8)
            color=cv2.applyColorMap(intensity,cv2.COLORMAP_TURBO)
            color[depth==0]=0
            if not cv2.imwrite(str(args.output/'depth_raw.png'),depth):
                raise RuntimeError('Cannot write raw depth PNG')
            if not cv2.imwrite(str(args.output/'depth_color.png'),color):
                raise RuntimeError('Cannot write colorized depth PNG')
            np.save(args.output/'depth_raw.npy',depth)
            intr=profile.as_video_stream_profile().get_intrinsics()
            report.update(ok=True,shape=list(depth.shape),depth_scale_m=scale,
                          valid_fraction=float(valid.size/depth.size),
                          min_m=float(valid.min()),median_m=float(np.median(valid)),max_m=float(valid.max()),
                          visualization_range_m=[.2,2.],
                          frame_number=frame.get_frame_number(),
                          timestamp_ms=frame.get_timestamp(),
                          intrinsics={'fx':intr.fx,'fy':intr.fy,'ppx':intr.ppx,'ppy':intr.ppy})
            attempt['ok']=True;success=True
            print(json.dumps(report,indent=2),flush=True)
        except Exception as exc:
            attempt.update(ok=False,error=str(exc))
            print(json.dumps(attempt),flush=True)
        finally:
            report['attempts'].append(attempt)
            if started:sensor.stop()
            if opened:sensor.close()
            (args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
        if success:break
        if args.low_power:break  # no further attempts on a possibly disconnected handle
        time.sleep(1)
    for option,value in saved_options.items():
        try:
            fresh=next(d for d in context.query_devices() if d.get_info(rs.camera_info.serial_number)==args.serial)
            fresh.first_depth_sensor().set_option(option,value)
        except Exception as exc:
            report.setdefault('restore_errors',[]).append(str(exc))
    report['low_power']=args.low_power
    if not success:
        report['ok']=False
        (args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
        raise SystemExit('D435i did not deliver a measured depth image; see report.json')
    (args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':main()
