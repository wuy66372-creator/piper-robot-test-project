"""Read-only RGB/depth smoke check; no robot connections or commands."""
import argparse
import importlib.metadata
import json
from pathlib import Path
import time

import numpy as np

from realsense_camera import RealSenseFrontStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial')
    parser.add_argument('--depth',action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    camera = RealSenseFrontStore(args.serial,depth_enabled=args.depth).connect()
    try:
        seen = set()
        ages = []
        deadline = time.monotonic()+3
        while time.monotonic()<deadline:
            _, _, meta = camera.frame()
            if not meta['available']:
                raise RuntimeError(f'D435i frame lost: {meta}')
            seen.add(meta['sequence'])
            ages.append(meta['age_upper_bound_s'])
            time.sleep(.03)
        if len(seen)<20:
            raise RuntimeError(f'D435i stream not advancing: {len(seen)} frames in 3 seconds')
        rgb, jpeg, meta = camera.frame()
        depth_report = {}
        if args.depth:
            with camera.lock:
                depth = camera.depth.copy()
            valid = depth[depth>0]*meta['depth_scale_m']
            if not valid.size:
                raise RuntimeError('D435i returned empty depth')
            depth_report = dict(depth_valid_fraction=float(np.count_nonzero(depth)/depth.size),
                                depth_median_m=float(np.median(valid)),depth_shape=list(depth.shape))
        args.output.mkdir(parents=True,exist_ok=True)
        (args.output/'color.jpg').write_bytes(jpeg)
        if args.depth:
            np.save(args.output/'aligned_depth.npy',depth)
        report = dict(meta, sdk_version=importlib.metadata.version('pyrealsense2'),
                      checked_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                      observed_frames=len(seen), duration_s=3, max_age_s=max(ages),
                      rgb_shape=list(rgb.shape), **depth_report)
        (args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report,indent=2))
    finally:
        camera.close()


if __name__=='__main__': main()
