"""Real HTTP regression suite, using a temporary DB/uploads and a separate server.

Fixture JWTs are created only in this test process; there is no development login API.
No live WeChat, payment, or model requests are made.
"""
import io
from contextlib import closing
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

import httpx
import jwt
from docx import Document
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='zhi-api-test-')
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.database = Path(cls.tmp.name) / 'data.db'
        cls.uploads = Path(cls.tmp.name) / 'uploads'
        cls.secret = secrets.token_urlsafe(48)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = {**os.environ, 'PYTHONPATH': str(ROOT), 'APP_ENV': 'test',
               'DATABASE_URL': 'sqlite+aiosqlite:///' + str(cls.database),
               'UPLOAD_DIR': str(cls.uploads), 'JWT_SECRET': cls.secret,
               'WECHAT_APPID': '', 'WECHAT_SECRET': '', 'WECHAT_MCHID': '',
               'DEEPSEEK_API_KEY': '', 'MINIMAX_API_KEY': '', 'HARNESS_ENABLED': 'false'}
        cls.log = open(Path(cls.tmp.name) / 'server.log', 'w+')
        cls.addClassCleanup(cls.log.close)
        cls.process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--port', str(port)],
                                       cwd=cls.tmp.name, env=env, stdout=cls.log, stderr=cls.log)
        cls.addClassCleanup(cls.stop_server)
        cls.client = httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=20, trust_env=False)
        cls.addClassCleanup(cls.client.close)
        for _ in range(80):
            try:
                if cls.client.get('/ready').status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if cls.process.poll() is not None:
                break
            time.sleep(0.1)
        cls.log.seek(0)
        raise RuntimeError(cls.log.read())

    @classmethod
    def stop_server(cls):
        cls.process.terminate()
        cls.process.wait(timeout=15)

    def setUp(self):
        self.user, self.headers = self.fixture_user()

    def tearDown(self):
        self.client.delete('/api/me', headers=self.headers)

    def fixture_user(self):
        user = uuid.uuid4().hex
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('INSERT INTO users(id,openid,nickname,created_at,updated_at) VALUES(?,?,?,?,?)',
                       (user, user, '测试用户', '2026-09-13', '2026-09-13'))
            db.commit()
        token = jwt.encode({'sub': user, 'exp': int(time.time()) + 600}, self.secret, algorithm='HS256')
        return user, {'Authorization': f'Bearer {token}'}

    def kb(self):
        response = self.client.get('/api/knowledge', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()
        self.assertTrue(items)
        return items[0]['id']

    def upload(self, kb, name='资料.txt', content=None):
        response = self.client.post(f'/api/knowledge/{kb}/documents', headers=self.headers,
                                    files={'file': (name, content if content is not None else '中文检索：合同编号 ABC-2026。'.encode())})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_health_and_auth(self):
        self.assertTrue(self.client.get('/health').json()['ok'])
        self.assertEqual(self.client.get('/api/knowledge').status_code, 401)
        self.assertEqual(self.client.get('/api/me', headers=self.headers).json()['id'], self.user)
        self.assertEqual(self.client.post('/api/auth/login', json={'code': 'test-only'}).status_code, 503)
        self.assertEqual(self.client.post('/api/pay/orders', headers=self.headers, json={'plan': 'pro_monthly'}).status_code, 503)
        self.assertEqual(self.client.post('/api/pay/notify', json={}).status_code, 400)

    def test_default_knowledge_is_created_once_and_sorted_first(self):
        first = self.client.get('/api/knowledge', headers=self.headers)
        self.assertEqual(first.status_code, 200, first.text)
        items = first.json()
        self.assertEqual(items[0]['name'], '微信用户的知识库')
        self.assertEqual(len(items), 1)

        created = self.client.post('/api/knowledge', headers=self.headers, json={'name': '项目资料'})
        self.assertEqual(created.status_code, 403, created.text)

        second = self.client.get('/api/knowledge', headers=self.headers)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual([item['name'] for item in second.json()], ['微信用户的知识库'])

    def test_document_search_retry_delete(self):
        kb = self.kb()
        doc = self.upload(kb)
        self.assertEqual(doc['status'], 'completed')
        found = self.client.get('/api/search', headers=self.headers, params={'knowledge_id': kb, 'q': '中文'}).json()
        self.assertEqual(found[0]['document_id'], doc['id'])
        for term in ['ABC-2026', '"', 'foo OR', '内容？', '(hello):']:
            self.assertEqual(self.client.get('/api/search', headers=self.headers, params={'knowledge_id': kb, 'q': term}).status_code, 200)
        self.assertIn('合同', self.client.get(f'/api/documents/{doc["id"]}/download', headers=self.headers).text)
        self.assertEqual(self.client.post(f'/api/documents/{doc["id"]}/retry', headers=self.headers).json()['status'], 'completed')
        self.assertEqual(self.client.delete(f'/api/documents/{doc["id"]}', headers=self.headers).status_code, 200)
        detail = self.client.get(f'/api/knowledge/{kb}', headers=self.headers).json()
        self.assertEqual(detail['documents'], [])
        self.assertEqual(detail['knowledge']['document_count'], 0)
        self.assertEqual(self.client.get(f'/api/documents/{doc["id"]}', headers=self.headers).status_code, 404)

    def test_document_tags_update_and_isolation(self):
        kb = self.kb()
        doc = self.upload(kb)
        response = self.client.patch(
            f'/api/documents/{doc["id"]}/tags',
            headers=self.headers,
            json={'tags': [' 项目 ', '项目', '合同', '', '这是一个超过二十四个字符的超长标签名称用于截断验证']},
        )
        self.assertEqual(response.status_code, 200, response.text)
        tags = response.json()['tags']
        self.assertEqual(tags[:2], ['项目', '合同'])
        self.assertEqual(len(tags[-1]), 24)

        _, other = self.fixture_user()
        try:
            self.assertEqual(self.client.patch(f'/api/documents/{doc["id"]}/tags', headers=other, json={'tags': ['越权']}).status_code, 404)
        finally:
            self.client.delete('/api/me', headers=other)

    def test_document_tags_auth_required(self):
        kb = self.kb()
        doc = self.upload(kb)
        self.assertEqual(self.client.patch(f'/api/documents/{doc["id"]}/tags', json={'tags': ['未登录']}).status_code, 401)

    def test_upload_returns_organization_state(self):
        kb = self.kb(); doc = self.upload(kb, content='项目预算为一百万元。交付时间是六月。')
        self.assertEqual(doc['organize_status'], 'processing')
        deadline = time.time() + 5
        detail = None
        while time.time() < deadline:
            detail = self.client.get(f'/api/documents/{doc["id"]}', headers=self.headers).json()
            if detail.get('organize_status') == 'completed':
                break
            time.sleep(0.1)
        self.assertEqual(detail['organize_status'], 'completed')
        self.assertTrue(detail['summary'])
        self.assertTrue(detail['key_points'])

    def test_docx_and_corrupt_document(self):
        kb = self.kb()
        docx = Document(); docx.add_paragraph('合同有效期三年')
        output = io.BytesIO(); docx.save(output)
        doc = self.upload(kb, '合同.docx', output.getvalue())
        self.assertEqual(doc['status'], 'completed')
        self.assertIn('三年', self.client.get(f'/api/documents/{doc["id"]}', headers=self.headers).json()['extracted_text'])
        broken = self.upload(kb, '损坏.pdf', b'not a PDF')
        self.assertEqual(broken['status'], 'failed')
        self.assertEqual(self.client.post(f'/api/documents/{broken["id"]}/retry', headers=self.headers).json()['status'], 'failed')
        self.assertEqual(self.client.post(f'/api/knowledge/{kb}/documents', headers=self.headers, files={'file': ('a.exe', b'bad')}).status_code, 415)

    def test_image_upload_preserves_client_filename(self):
        kb = self.kb()
        image = Image.new('RGB', (48, 48), 'white')
        content = io.BytesIO(); image.save(content, format='JPEG')
        response = self.client.post(
            f'/api/knowledge/{kb}/documents', headers={**self.headers, 'X-Upload-Filename': '%E6%89%AB%E6%8F%8F%E8%B5%84%E6%96%99.jpg'},
            files={'file': ('wxfile', content.getvalue())},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['filename'], '扫描资料.jpg')
        self.assertEqual(response.json()['status'], 'completed')

    def test_stream_and_knowledge_deletion(self):
        kb = self.kb(); doc = self.upload(kb)
        response = self.client.post('/api/chat/stream', headers=self.headers, json={'knowledge_id': kb, 'content': '合同？'})
        self.assertEqual(response.status_code, 200, response.text)
        events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]
        self.assertEqual(events[0]['type'], 'meta'); self.assertEqual(events[-1]['type'], 'done')
        self.assertEqual(events[0]['sources'][0]['document_id'], doc['id'])
        self.assertIn('尚未完成配置', ''.join(e.get('content', '') for e in events))
        conversation = events[0]['conversation_id']
        self.assertEqual(len(self.client.get(f'/api/conversations/{conversation}', headers=self.headers).json()), 2)
        self.client.delete(f'/api/knowledge/{kb}', headers=self.headers).raise_for_status()
        self.assertEqual(self.client.get('/api/conversations', headers=self.headers).json(), [])
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM chunks_fts WHERE knowledge_id=?', (kb,)).fetchone()[0], 0)
        self.assertFalse(any(self.uploads.glob(doc['id'] + '_*')))

    def test_stream_without_documents_stays_in_current_knowledge(self):
        kb = self.kb()
        response = self.client.post('/api/chat/stream', headers=self.headers, json={'knowledge_id': kb, 'content': '帮我写一句欢迎语'})
        self.assertEqual(response.status_code, 200, response.text)
        events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]
        self.assertEqual(events[0]['type'], 'meta')
        self.assertEqual(events[0]['sources'], [])
        self.assertEqual(events[-1]['type'], 'done')
        conversation = events[0]['conversation_id']
        stored = self.client.get(f'/api/conversations/{conversation}', headers=self.headers).json()
        self.assertEqual(len(stored), 2)
        self.assertEqual(stored[0]['content'], '帮我写一句欢迎语')

    def test_cross_user_isolation(self):
        kb = self.kb(); doc = self.upload(kb)
        _, other = self.fixture_user()
        try:
            for path in [f'/api/knowledge/{kb}', f'/api/documents/{doc["id"]}', f'/api/documents/{doc["id"]}/download']:
                self.assertEqual(self.client.get(path, headers=other).status_code, 404)
            self.assertEqual(self.client.delete(f'/api/knowledge/{kb}', headers=other).status_code, 404)
            other_kb = self.client.get('/api/knowledge', headers=other).json()[0]['id']
            stream = self.client.post('/api/chat/stream', headers=other, json={'knowledge_id': other_kb, 'content': '测试'})
            conversation = json.loads(stream.text.splitlines()[0][5:])['conversation_id']
            attempt = self.client.post('/api/chat/stream', headers=self.headers, json={'knowledge_id': kb, 'conversation_id': conversation, 'content': '越权测试'})
            self.assertEqual(attempt.status_code, 404)
        finally:
            self.client.delete('/api/me', headers=other)

    def test_account_deletion_invalidates_token(self):
        kb = self.kb(); doc = self.upload(kb)
        self.client.delete('/api/me', headers=self.headers).raise_for_status()
        self.assertEqual(self.client.get('/api/me', headers=self.headers).status_code, 401)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM chunks_fts WHERE knowledge_id=?', (kb,)).fetchone()[0], 0)
        self.assertFalse(any(self.uploads.glob(doc['id'] + '_*')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
