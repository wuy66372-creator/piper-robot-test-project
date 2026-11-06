"""Download the pinned official legacy SDK into the machine-local cache."""
import hashlib
import os
from pathlib import Path
import stat
import urllib.request
import zipfile
cache = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'hackathon/orbbec-v1.10.37'
cache.mkdir(parents=True, exist_ok=True)
archive = cache / 'sdk.zip'
url = 'https://github.com/orbbec/OrbbecSDK/releases/download/v1.10.37/OrbbecSDK_C_C%2B%2B_v1.10.37_20260707_3f75820_linux_x64_release.zip'
if not archive.exists():
    urllib.request.urlretrieve(url, archive)
expected = '0a8921e659ef172a535ec1631379d29c2542ad3afabe2b5592d20be1db0158c8'
if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
    raise SystemExit('SDK archive hash mismatch')
dest = cache / 'sdk'
with zipfile.ZipFile(archive) as z:
    for item in z.infolist():
        target = dest / item.filename
        if not target.resolve().is_relative_to(dest.resolve()):
            raise SystemExit('Unsafe archive path')
        if stat.S_ISLNK(item.external_attr >> 16):
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                target.unlink()
            target.symlink_to(z.read(item).decode())
        else:
            z.extract(item, dest)
print(dest)
