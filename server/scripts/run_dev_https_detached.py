"""以独立会话启动开发用 HTTPS 资源服务（避免被调用方的进程组清理杀掉）。

用法: python3 scripts/run_dev_https_detached.py [端口，默认 9443]
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, 'logs', 'dev_https.log')
PYTHON = '/Users/mac/.workbuddy/binaries/python/versions/3.13.12/bin/python3'
PYTHONPATH = os.path.join(ROOT, '.venv/lib/python3.13/site-packages')


def main() -> None:
    port = sys.argv[1] if len(sys.argv) > 1 else '9443'
    pid = os.fork()
    if pid > 0:
        print(f'detached launcher parent exit, child pid={pid} (https asset server :{port})')
        return
    os.setsid()
    env = dict(os.environ)
    env['PYTHONPATH'] = PYTHONPATH
    log = open(LOG, 'ab', buffering=0)
    subprocess.Popen(
        [PYTHON, os.path.join(ROOT, 'scripts', 'run_dev_https.py'), '--port', port],
        cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        start_new_session=True,
    )


if __name__ == '__main__':
    main()
    sys.exit(0)
