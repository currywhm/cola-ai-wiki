"""Local service manager. All paths are relative to this backend, never the shell cwd."""
import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import venv

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve()
# `.venv-x86` is used by the local Mac checkout when the bundled `.venv`
# was created for a different CPU architecture. Deployments continue to use `.venv`.
PYTHON = ROOT / '.venv-x86' / 'bin' / 'python' if (ROOT / '.venv-x86' / 'bin' / 'python').exists() else ROOT / '.venv' / 'bin' / 'python'
STATE = ROOT / '.run' / 'service.json'
LOG = ROOT / 'logs' / 'api.log'


def setup():
    if sys.version_info < (3, 11):
        raise SystemExit('Python 3.11+ is required')
    env = ROOT / '.env'
    if not env.exists():
        content = (ROOT / '.env.example').read_text().replace('replace-with-a-long-random-secret', secrets.token_urlsafe(48))
        fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as target:
            target.write(content)
    if not PYTHON.exists():
        venv.EnvBuilder(with_pip=True).create(ROOT / '.venv')
    subprocess.run([str(PYTHON), '-m', 'pip', 'install', '-r', str(ROOT / 'requirements.txt')], check=True)
    print('Setup complete. Configure WeChat/model credentials in .env. No credentials are printed.')


def state():
    if not STATE.exists():
        return None
    value = json.loads(STATE.read_text())
    result = subprocess.run(['ps', '-p', str(value['pid']), '-o', 'command='], capture_output=True, text=True)
    if str(SCRIPT) not in result.stdout or '_serve' not in result.stdout:
        return None
    return value


def ready(port):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/ready', timeout=1) as response:
            return json.load(response).get('database') == 'ready'
    except (OSError, ValueError):
        return False


def start(port):
    existing = state()
    if existing:
        print(json.dumps({**existing, 'ready': ready(existing['port']), 'log': str(LOG)}))
        return
    if not PYTHON.exists() or not (ROOT / '.env').exists():
        raise SystemExit('Run: python3 scripts/manage.py setup')
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', port))
        except OSError:
            raise SystemExit(f'Port {port} is already in use. Choose --port; existing processes are not stopped.')
    STATE.parent.mkdir(exist_ok=True)
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open('ab') as log:
        process = subprocess.Popen([str(PYTHON), str(SCRIPT), '_serve', '--port', str(port)], cwd=ROOT,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    STATE.write_text(json.dumps({'pid': process.pid, 'port': port}))
    for _ in range(60):
        if process.poll() is not None:
            raise SystemExit(f'Start failed; see {LOG}')
        if ready(port):
            print(f'Backend running: http://127.0.0.1:{port} (PID {process.pid})\nLog: {LOG}')
            return
        time.sleep(0.25)
    raise SystemExit(f'Startup not ready yet; check {LOG}')


def stop():
    current = state()
    if not current:
        print('Service is not running')
        return
    os.kill(current['pid'], signal.SIGTERM)
    for _ in range(60):
        if not state():
            STATE.unlink(missing_ok=True)
            print('Service stopped')
            return
        time.sleep(0.25)
    raise SystemExit('Graceful stop is still pending; see logs')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['setup', 'start', 'stop', 'status', 'run', '_serve'])
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if args.action == 'setup':
        setup()
    elif args.action == 'start':
        start(args.port)
    elif args.action == 'stop':
        stop()
    elif args.action == 'status':
        current = state()
        print(json.dumps({**(current or {}), 'running': bool(current), 'ready': bool(current and ready(current['port'])), 'log': str(LOG)}))
    elif args.action == 'run':
        os.execv(str(PYTHON), [str(PYTHON), str(SCRIPT), '_serve', '--port', str(args.port)])
    else:
        sys.path.insert(0, str(ROOT))
        import uvicorn
        uvicorn.run('app.main:app', host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
