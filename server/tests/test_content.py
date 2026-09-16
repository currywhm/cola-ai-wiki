"""使用技巧「源文件 → 数据库」的导入回归测试。

不在测试进程里 import app.db，避免污染其他用例的数据库配置：整段逻辑跑在子进程里，
用临时数据库和临时内容目录，断言内容确实落库、可重复执行、删除的条目会被清理。
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

RUNNER = r'''
import asyncio, json, os
from pathlib import Path

from app.db import init_db
from app.services.content import ContentError, asset, tip_entry, tips_index
from app.services.content_import import ensure_content_imported, import_tips

root = Path(os.environ['CONTENT_ROOT'])
PNG = b'\x89PNG\r\n\x1a\n' + b'0' * 16


def write_manifest(entries):
    (root / 'manifest.json').write_text(json.dumps({
        'schema': 1, 'version': 'test-1', 'title': '使用技巧', 'subtitle': '测试',
        'updated_at': '2026-09-16', 'intro': '测试用', 'groups': [
            {'id': 'g1', 'title': '组一', 'entries': entries},
        ],
    }, ensure_ascii=False), encoding='utf-8')


def entry(entry_id, title, body):
    return {'id': entry_id, 'title': title, 'summary': '摘要', 'icon': 'dengpao',
            'cover': 'fig.png', 'body': body, 'read_minutes': 3}


async def main():
    await init_db()
    (root / 'entries').mkdir(parents=True)
    (root / 'assets').mkdir(parents=True)
    (root / 'entries' / 'demo.md').write_text(
        '## 标题\n\n正文一句。\n\n1. 第一步\n\n![图](fig.png)\n\n> 提示\n', encoding='utf-8')
    (root / 'assets' / 'fig.png').write_bytes(PNG)
    write_manifest([entry('demo', '演示', 'entries/demo.md'), entry('gone', '待删除', 'entries/demo.md')])
    (root / 'entries' / 'gone.md').write_text('## 待删除\n', encoding='utf-8')
    write_manifest([entry('demo', '演示', 'entries/demo.md'), entry('gone', '待删除', 'entries/gone.md')])

    first = await import_tips(root=root)
    assert first['skipped'] is False and first['entries'] == 2 and first['assets'] == 1, first
    again = await import_tips(root=root)
    assert again['skipped'] is True, again
    forced = await import_tips(root=root, force=True)
    assert forced['skipped'] is False, forced
    assert await ensure_content_imported() == []  # 库里已有内容，启动兜底不重复导入

    index = await tips_index()
    assert index['version'] == 'test-1' and index['title'] == '使用技巧', index
    assert [e['id'] for g in index['groups'] for e in g['entries']] == ['demo', 'gone']
    assert index['groups'][0]['entries'][0]['cover'] == '/api/content/assets/fig.png'

    detail = await tip_entry('demo')
    assert detail['group'] == '组一', detail
    assert [b['type'] for b in detail['blocks']] == ['heading', 'paragraph', 'step', 'figure', 'note'], detail['blocks']
    assert [b['src'] for b in detail['blocks'] if b['type'] == 'figure'] == ['/api/content/assets/fig.png']
    assert await tip_entry('unknown') is None

    blob = await asset('fig.png')
    assert blob['mime'] == 'image/png' and blob['bytes'] == PNG and blob['size'] == len(PNG), blob
    for bad in ('../.env', 'sub/fig.png', '.hidden.png', 'fig.txt', 'missing.png'):
        assert await asset(bad) is None, bad

    # 清单里删掉条目、目录里删掉配图后重新导入：库里不能残留旧内容
    (root / 'assets' / 'fig.png').unlink()
    write_manifest([entry('demo', '演示', 'entries/demo.md')])
    pruned = await import_tips(root=root, force=True)
    assert pruned['entries'] == 1 and pruned['assets'] == 0, pruned
    after = await tips_index()
    assert [e['id'] for g in after['groups'] for e in g['entries']] == ['demo'], after
    assert after['groups'][0]['entries'][0]['cover'] == '', after
    assert await asset('fig.png') is None

    # 源文件缺失时报明确错误，而不是静默成功
    empty = root.parent / 'empty'
    empty.mkdir(exist_ok=True)
    try:
        await import_tips(root=empty)
    except ContentError as exc:
        assert '文案源文件缺失' in str(exc), exc
    else:
        raise AssertionError('缺少清单时应报错')

    print(json.dumps({'ok': True, 'entries': pruned['entries'], 'version': after['version']}, ensure_ascii=False))


asyncio.run(main())
'''


class ContentImportTests(unittest.TestCase):
    def test_source_files_are_imported_into_database(self):
        with tempfile.TemporaryDirectory(prefix='zhi-content-') as tmp:
            base = Path(tmp)
            source = base / 'content' / 'tips'
            source.mkdir(parents=True)
            env = {
                **os.environ,
                'PYTHONPATH': str(ROOT),
                'DATABASE_URL': 'sqlite+aiosqlite:///' + str(base / 'content.db'),
                'CONTENT_ROOT': str(source),
                # 内容根目录也指到临时目录，避免把仓库里真实的 content/ 导进临时库
                'CONTENT_DIR': str(base / 'content'),
                'APP_ENV': 'test',
            }
            result = subprocess.run([sys.executable, '-c', RUNNER], env=env, cwd=tmp, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertTrue(payload['ok'])
            self.assertEqual(payload['entries'], 1)


if __name__ == '__main__':
    unittest.main()
