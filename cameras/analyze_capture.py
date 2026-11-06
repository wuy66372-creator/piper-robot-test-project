"""Validate captured RGB/depth files and save metric depth and a preview."""
import json
from pathlib import Path
import sys
import numpy as np
from PIL import Image

def analyze(directory):
    directory = Path(directory)
    metadata = json.loads((directory / 'capture.json').read_text())
    mapping = json.loads((Path(__file__).parent / 'mapping.json').read_text())
    roles = {value['serial']: role for role, value in mapping.items() if isinstance(value, dict)}
    report = {'aligned': False, 'hardware_synchronized': False, 'cameras': []}
    for camera in metadata['cameras']:
        serial = camera['serial']
        depth = camera['depth']
        raw = np.fromfile(directory / f'{serial}_depth_u16le.bin', dtype='<u2')
        if raw.size != depth['width'] * depth['height']:
            raise ValueError(f'{serial}: wrong depth frame size')
        mm = raw.reshape(depth['height'], depth['width']).astype(np.float32) * depth['scale_mm']
        valid = mm > 0
        if not valid.any():
            raise ValueError(f'{serial}: no valid depth')
        color = Image.open(directory / f'{serial}_color.jpg')
        if color.size != (camera['color']['width'], camera['color']['height']):
            raise ValueError(f'{serial}: wrong color dimensions')
        np.save(directory / f'{serial}_depth_mm.npy', mm)
        # Grayscale display only; raw metric values remain in .npy. Zero = invalid.
        preview = np.where(valid, np.clip(mm / 2000 * 255, 1, 255), 0).astype(np.uint8)
        Image.fromarray(preview).save(directory / f'{serial}_depth_preview.png')
        stats = {'serial': serial, 'wrist': roles[serial], 'valid_fraction': float(valid.mean()),
                 'min_mm': float(mm[valid].min()), 'median_mm': float(np.median(mm[valid])),
                 'max_mm': float(mm[valid].max()),
                 'depth_host_fps': depth['observed_host_fps'],
                 'color_host_fps': camera['color']['observed_host_fps']}
        report['cameras'].append(stats)
    (directory / 'validation.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))

if __name__ == '__main__':
    analyze(sys.argv[1])
