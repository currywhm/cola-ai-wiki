"""Build a source-only deployment archive; never include local credentials or user data."""
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'dist' / 'zhi-reader-api.tar.gz'
# content 是使用技巧等运营文案的源文件：部署后由 scripts/import_content.py
# （或服务首次启动的自动导入）写进数据库，因此必须随包发布。
FILES = ['app', 'scripts', 'tests', 'deploy', 'content', 'requirements.txt', 'Dockerfile',
         'docker-compose.yml', '.env.example', '.dockerignore', '.gitignore', 'README.md', 'VALIDATION.md']


def main():
    OUTPUT.parent.mkdir(exist_ok=True)
    with tarfile.open(OUTPUT, 'w:gz') as archive:
        for name in FILES:
            source = ROOT / name
            for path in sorted(source.rglob('*')) if source.is_dir() else [source]:
                if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                    archive.add(path, arcname=str(Path('zhi-reader-api') / path.relative_to(ROOT)), recursive=False)
    print(OUTPUT)


if __name__ == '__main__':
    main()
