"""Start/stop only the model process group created by this local helper."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
from model_profiles import resolve

ROOT = Path(__file__).resolve().parents[1]
CACHE = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'hackathon'
PIDFILE = CACHE / 'show-harness-vllm-service.json'


def start_ticks(pid):
    return Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]


def stop():
    if not PIDFILE.exists():
        print('No managed model service'); return
    data = json.loads(PIDFILE.read_text())
    pid = data['pid']
    try:
        command = Path(f'/proc/{pid}/cmdline').read_bytes()
    except FileNotFoundError:
        PIDFILE.unlink(); print('Model service already stopped'); return
    if (b'vllm.entrypoints.cli.main' not in command or os.getpgid(pid) != pid
            or start_ticks(pid) != data.get('start_ticks')):
        raise SystemExit('Recorded PID is not our model process group; refusing to signal')
    os.killpg(pid, signal.SIGTERM)
    for _ in range(200):
        path = Path(f'/proc/{pid}/stat')
        if not path.exists() or path.read_text().split()[2] == 'Z':
            PIDFILE.unlink(); print('Model service stopped'); return
        time.sleep(.1)
    raise SystemExit('Model process has not exited; inspect its log before retrying')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('start','stop'))
    parser.add_argument('profile',nargs='?',default='qwen3_5_2b')
    args = parser.parse_args()
    if args.command == 'stop':
        stop(); return
    resolve(args.profile)
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1',8000)) == 0:
            raise SystemExit('Port 8000 is already occupied; do not start a second model')
    if PIDFILE.exists():
        pid = json.loads(PIDFILE.read_text())['pid']
        if Path(f'/proc/{pid}/cmdline').exists() and Path(f'/proc/{pid}/cmdline').read_bytes():
            raise SystemExit('A managed model is starting/running; use models.sh stop first')
    CACHE.mkdir(parents=True,exist_ok=True)
    log_path = CACHE / f'show-harness-vllm-{args.profile}.log'
    with log_path.open('a') as log:
        proc = subprocess.Popen(['bash',str(ROOT / 'show_harness/models.sh'),'serve',args.profile],
                                stdout=log,stderr=log,start_new_session=True,cwd=ROOT)
    time.sleep(.5)
    if proc.poll() is not None:
        raise SystemExit(f'Model exited; inspect {log_path}')
    PIDFILE.write_text(json.dumps({'pid':proc.pid,'profile':args.profile,'log':str(log_path),
                                  'start_ticks':start_ticks(proc.pid)})+'\n')
    print(f'Model starting: {args.profile}; PID {proc.pid}; log {log_path}')
    print('Check readiness with bash show_harness/models.sh status')


if __name__ == '__main__': main()
