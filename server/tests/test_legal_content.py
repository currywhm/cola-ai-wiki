"""合规文档「源文件 → 数据库」的回归测试。

和 test_content.py 同一套路：跑在子进程里，用临时数据库和临时内容目录，
重点验证那些「上线会被抓」的点：

* 占位符（运营者 / AppID / 备案号 / 邮箱）在导入时就替换干净，库里不留 ``{{xxx}}``；
* 正文里写了未知占位符会直接报错，而不是把半成品文案发到线上；
* 有序步骤按小标题重新编号，不跨节连号；
* 更新日期 / 生效日期随条目存库并能下发。
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
from app.services.content import ContentError, legal_doc, legal_index
from app.services.content_import import apply_placeholders, import_content, import_warnings

root = Path(os.environ['CONTENT_ROOT'])
source = root / 'legal'

BODY = "\n".join([
    "> 更新日期：2026年9月16日 · 生效日期：2026年9月16日",
    "",
    "## 联系我们",
    "",
    "运营者：{{operator}}。AppID：{{appid}}。",
    "",
    "{{icp_line}}",
    "",
    "{{email_line}}",
    "",
    "## 账号注销",
    "",
    "1. 第一步",
    "",
    "1. 第二步",
    "",
])


def write_manifest():
    (source / 'manifest.json').write_text(json.dumps({
        'schema': 1, 'version': 'test-legal-1', 'title': '服务说明', 'subtitle': '测试',
        'updated_at': '2026-09-16', 'intro': '测试用', 'groups': [
            {'id': 'g1', 'title': '组一', 'entries': [{
                'id': 'guide', 'title': '小程序隐私保护指引', 'summary': '摘要', 'icon': 'dengpao',
                'cover': '', 'body': 'entries/guide.md', 'read_minutes': 5,
                'updated_at': '2026年9月16日', 'effective_at': '2026年9月16日',
            }]},
        ],
    }, ensure_ascii=False), encoding='utf-8')


async def main():
    await init_db()
    (source / 'entries').mkdir(parents=True)
    (source / 'entries' / 'guide.md').write_text(BODY, encoding='utf-8')
    write_manifest()

    first = await import_content('legal', root=source)
    assert first['skipped'] is False and first['entries'] == 1, first
    assert (await import_content('legal', root=source))['skipped'] is True  # 指纹未变即跳过

    index = await legal_index()
    assert index['version'] == 'test-legal-1', index
    assert [doc['id'] for doc in index['docs']] == ['guide'], index
    assert index['docs'][0]['updated_at'] == '2026年9月16日', index
    assert index['docs'][0]['effective_at'] == '2026年9月16日', index

    doc = await legal_doc('guide')
    text = ' '.join(''.join(run['text'] for run in block.get('runs', [])) or block.get('text', '') for block in doc['blocks'])
    assert '{{' not in text, text
    assert os.environ['LEGAL_OPERATOR_NAME'] in text, text
    assert os.environ['LEGAL_ICP_NUMBER'] in text, text
    assert os.environ['LEGAL_CONTACT_EMAIL'] in text, text
    assert await legal_doc('missing') is None

    steps = [(b['index'], b['runs'][0]['text']) for b in doc['blocks'] if b['type'] == 'step']
    assert steps == [(1, '第一步'), (2, '第二步')], steps

    try:
        apply_placeholders('运营者：{{unknown_field}}')
    except ContentError as exc:
        assert 'unknown_field' in str(exc), exc
    else:
        raise AssertionError('未知占位符必须报错')

    assert isinstance(import_warnings(), list)
    print(json.dumps({'ok': True, 'docs': len(index['docs'])}, ensure_ascii=False))


asyncio.run(main())
'''


class LegalContentTests(unittest.TestCase):
    def test_legal_source_files_are_imported_with_placeholders_resolved(self):
        with tempfile.TemporaryDirectory(prefix='zhi-legal-') as tmp:
            base = Path(tmp)
            (base / 'content' / 'legal').mkdir(parents=True)
            env = {
                **os.environ,
                'PYTHONPATH': str(ROOT),
                'DATABASE_URL': 'sqlite+aiosqlite:///' + str(base / 'legal.db'),
                'CONTENT_ROOT': str(base / 'content'),
                'CONTENT_DIR': str(base / 'content'),
                'LEGAL_OPERATOR_NAME': '某某某',
                'LEGAL_CONTACT_EMAIL': 'hi@example.com',
                'LEGAL_ICP_NUMBER': '粤ICP备2026000000号',
                'APP_ENV': 'test',
            }
            result = subprocess.run([sys.executable, '-c', RUNNER], env=env, cwd=tmp, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertTrue(payload['ok'])
            self.assertEqual(payload['docs'], 1)


if __name__ == '__main__':
    unittest.main()
