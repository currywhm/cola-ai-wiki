"""以独立会话启动后端，避免被调用方的进程组清理杀掉。

用法: python3 scripts/run_detached.py
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, 'logs', 'api.log')
PYTHON = '/Users/mac/.workbuddy/binaries/python/versions/3.13.12/bin/python3'
# 本机 venv 的 site-packages 需要显式注入（pyvenv.cfg 指向的 base 已不存在）。
# deepseek-harness 的 SDK 路径由 .env 的 HARNESS_SDK_PATH / HARNESS_RUNTIME_SDK_PATH 提供，
# 部署机建议 pip install deepseek-harness-sdk，无需这两项配置。
PYTHONPATH = os.path.join(ROOT, '.venv/lib/python3.13/site-packages')


def main() -> None:
    pid = os.fork()
    if pid > 0:
        print(f'detached launcher parent exit, child pid={pid}')
        return
    os.setsid()
    env = dict(os.environ)
    env['PYTHONPATH'] = PYTHONPATH
    # 清掉调用方注入的本地代理，避免 harness 子进程被代理拦截
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy'):
        env.pop(key, None)
    log = open(LOG, 'ab', buffering=0)
    subprocess.Popen(
        [PYTHON, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8765'],
        cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        start_new_session=True,
    )


if __name__ == '__main__':
    main()
    sys.exit(0)
