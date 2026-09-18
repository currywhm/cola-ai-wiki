"""Verify an already built image with disposable volumes, without using local .env/data."""
import argparse
import json
from pathlib import Path
import secrets
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def docker(*args, capture=True):
    result = subprocess.run(['docker', *args], check=True, text=True, capture_output=capture)
    return result.stdout.strip() if capture else ''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='zhi-reader-api:verify')
    args = parser.parse_args()
    docker(
        'run', '--rm',
        '-e', 'APP_ENV=test',
        '-e', 'STORAGE_BACKEND=local',
        '-e', 'HARNESS_ENABLED=false',
        '-v', f'{ROOT / "tests"}:/app/tests:ro',
        '-v', f'{ROOT / ".env.cloud.example"}:/app/.env.cloud.example:ro',
        args.image,
        'python', '-m', 'unittest', 'discover', '-s', '/app/tests', '-v', capture=False,
    )
    name = 'zhi-api-verify-' + uuid.uuid4().hex[:10]
    data, uploads = name + '-data', name + '-uploads'
    created_volumes = []
    launched = False

    def launch():
        # Random verification secret is never sent to the real server or printed.
        docker('run', '-d', '--name', name, '-e', 'APP_ENV=test', '-e', 'JWT_SECRET=' + secrets.token_urlsafe(48),
               '-e', 'STORAGE_BACKEND=local', '-e', 'HARNESS_ENABLED=false', '-e', 'PORT=8000',
               '-v', data + ':/app/data', '-v', uploads + ':/app/uploads', args.image)

    def ready():
        for _ in range(30):
            status = subprocess.run(['docker', 'exec', name, 'python', '-c',
                "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2)"],
                capture_output=True)
            if status.returncode == 0:
                return
            time.sleep(0.5)
        raise RuntimeError('Container readiness failed')

    try:
        for volume in [data, uploads]:
            docker('volume', 'create', '--label', 'zhi-reader-api.verification=true', volume)
            created_volumes.append(volume)
        launch(); launched = True; ready()
        assert docker('exec', name, 'id', '-u') == '10001'
        langs = docker('exec', name, 'tesseract', '--list-langs')
        assert 'chi_sim' in langs and 'eng' in langs
        docker('exec', name, 'python', '-c',
               "from pathlib import Path; Path('/app/data/verification').write_text('persist'); Path('/app/uploads/verification').write_text('persist')")
        docker('rm', '-f', name); launched = False
        launch(); launched = True; ready()
        docker('exec', name, 'python', '-c',
               "from pathlib import Path; assert Path('/app/data/verification').read_text() == 'persist'; assert Path('/app/uploads/verification').read_text() == 'persist'")
        config = json.loads(docker('inspect', name))[0]
        assert config['Config']['Healthcheck']['Test']
        print('Container PASS: HTTP suite, readiness, non-root UID, OCR languages, data/uploads after recreation, healthcheck configured')
    finally:
        if launched:
            docker('rm', '-f', name)
        for volume in created_volumes:
            docker('volume', 'rm', volume)


if __name__ == '__main__':
    main()
