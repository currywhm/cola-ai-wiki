"""开发环境用的 HTTPS 资源服务。

微信基础库 3.17 起 <image> 不再接受 http:// 图片地址（服务端下发的使用技巧配图都在
后端 /api/content/assets/*），本地调试因此什么都看不到。这个脚本用自签证书在
9443 端口起同一套 ASGI 应用，把图片走 https 出去；线上是正常 HTTPS 域名，不受影响。

    python3 scripts/run_dev_https.py [--port 9443] [--host 127.0.0.1]

证书不存在时会用 openssl 自动生成到 certs/ 目录（仅本机调试用，不要提交）。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CERTS = ROOT / 'certs'
KEY = CERTS / 'dev-127.0.0.1-key.pem'
CERT = CERTS / 'dev-127.0.0.1-cert.pem'


def ensure_certificate() -> None:
    if KEY.exists() and CERT.exists():
        return
    CERTS.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
            '-keyout', str(KEY), '-out', str(CERT), '-days', '825',
            '-subj', '/CN=127.0.0.1',
            '-addext', 'subjectAltName=IP:127.0.0.1,DNS:localhost',
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f'generated self-signed certificate: {CERT}')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=9443)
    args = parser.parse_args()

    ensure_certificate()
    sys.path.insert(0, str(ROOT))
    # 本机 .venv 的解释器已损坏，依赖装在 .venv/lib/python3.x/site-packages，这里手动补齐
    for candidate in sorted((ROOT / '.venv' / 'lib').glob('python*/site-packages')):
        sys.path.append(str(candidate))
    import uvicorn  # 延迟导入：让证书生成失败时先看到清晰错误

    print(f'https asset server: https://{args.host}:{args.port}  (self-signed, dev only)')
    uvicorn.run('app.main:app', host=args.host, port=args.port, ssl_keyfile=str(KEY), ssl_certfile=str(CERT), log_level='warning')


if __name__ == '__main__':
    main()
