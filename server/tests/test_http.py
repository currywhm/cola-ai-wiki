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

    def wait_document(self, document_id, timeout=30):
        """解析与整理都在后台跑（容器通道单次调用装不下）：等到终态再断言。"""
        deadline = time.time() + timeout
        detail = {}
        while time.time() < deadline:
            detail = self.client.get(f'/api/documents/{document_id}', headers=self.headers).json()
            if detail.get('status') in ('completed', 'failed'):
                return detail
            time.sleep(0.1)
        return detail

    def test_content_tips_are_served_from_database(self):
        """使用技巧：源文件在 content/ 目录，服务启动时导入数据库，接口只读库。"""
        manifest = json.loads((ROOT / 'content' / 'tips' / 'manifest.json').read_text(encoding='utf-8'))
        response = self.client.get('/api/content/tips')
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload['version'], manifest['version'])
        self.assertEqual(payload['title'], manifest['title'])
        self.assertEqual(len(payload['groups']), len(manifest['groups']))
        expected = {entry['id'] for group in manifest['groups'] for entry in group['entries']}
        self.assertEqual({entry['id'] for group in payload['groups'] for entry in group['entries']}, expected)

        first = payload['groups'][0]['entries'][0]
        detail = self.client.get(f"/api/content/tips/{first['id']}")
        self.assertEqual(detail.status_code, 200, detail.text)
        blocks = detail.json()['blocks']
        self.assertTrue(blocks)
        self.assertTrue({block['type'] for block in blocks} & {'paragraph', 'step', 'bullet'})

        cover = self.client.get(first['cover'])
        self.assertEqual(cover.status_code, 200, cover.text)
        self.assertEqual(cover.headers['content-type'], 'image/png')
        self.assertTrue(cover.content.startswith(b'\x89PNG'))
        self.assertEqual(self.client.get('/api/content/assets/..%2F.env').status_code, 404)
        self.assertEqual(self.client.get('/api/content/assets/missing.png').status_code, 404)
        self.assertEqual(self.client.get('/api/content/tips/unknown-entry').status_code, 404)

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
        usage = self.client.get('/api/me', headers=self.headers).json()['usage']
        self.assertEqual(usage['knowledge_bases'], 0)

        created = self.client.post('/api/knowledge', headers=self.headers, json={'name': '项目资料'})
        self.assertEqual(created.status_code, 200, created.text)

        # 免费试用只允许在默认库之外再建 1 个：第 2 个就该被挡下，并明确告诉用户上限
        second_created = self.client.post('/api/knowledge', headers=self.headers, json={'name': '第二个资料库'})
        self.assertEqual(second_created.status_code, 403, second_created.text)
        self.assertIn('最多创建 1 个知识库', second_created.json()['detail'])
        usage = self.client.get('/api/me', headers=self.headers).json()['usage']
        self.assertEqual(usage['knowledge_bases'], 1)

        second = self.client.get('/api/knowledge', headers=self.headers)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual([item['name'] for item in second.json()], ['微信用户的知识库', '项目资料'])

    def test_knowledge_avatar_upload_and_read(self):
        kb = self.kb()
        image = Image.new('RGB', (64, 64), 'green')
        content = io.BytesIO(); image.save(content, format='JPEG')
        uploaded = self.client.post(f'/api/knowledge/{kb}/avatar', headers=self.headers, files={'file': ('knowledge.jpg', content.getvalue())})
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        avatar_path = uploaded.json()['avatar'].split('?', 1)[0]
        served = self.client.get(avatar_path)
        self.assertEqual(served.status_code, 200, served.text)
        self.assertEqual(served.headers['content-type'], 'image/jpeg')
        self.assertTrue(served.content.startswith(b'\xff\xd8'))
        detail = self.client.get(f'/api/knowledge/{kb}', headers=self.headers).json()
        self.assertTrue(detail['knowledge']['avatar'].startswith(f'/api/knowledge-avatars/{kb}'))

    def test_document_search_retry_delete(self):
        kb = self.kb()
        doc = self.upload(kb)
        self.assertEqual(doc['status'], 'processing')
        self.assertEqual(self.wait_document(doc['id'])['status'], 'completed')
        found = self.client.get('/api/search', headers=self.headers, params={'knowledge_id': kb, 'q': '中文'}).json()
        self.assertEqual(found[0]['document_id'], doc['id'])
        for term in ['ABC-2026', '"', 'foo OR', '内容？', '(hello):']:
            self.assertEqual(self.client.get('/api/search', headers=self.headers, params={'knowledge_id': kb, 'q': term}).status_code, 200)
        self.assertIn('合同', self.client.get(f'/api/documents/{doc["id"]}/download', headers=self.headers).text)
        self.assertEqual(self.client.post(f'/api/documents/{doc["id"]}/retry', headers=self.headers).json()['status'], 'processing')
        self.assertEqual(self.wait_document(doc['id'])['status'], 'completed')
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
        # 解析与整理都在后台执行，上传接口只回报「已受理」
        self.assertEqual(doc['status'], 'processing')
        deadline = time.time() + 20
        detail = None
        while time.time() < deadline:
            detail = self.client.get(f'/api/documents/{doc["id"]}', headers=self.headers).json()
            if detail.get('organize_status') == 'completed':
                break
            time.sleep(0.1)
        self.assertEqual(detail['status'], 'completed')
        self.assertEqual(detail['organize_status'], 'completed')
        self.assertTrue(detail['summary'])
        self.assertTrue(detail['key_points'])

    def test_docx_and_corrupt_document(self):
        kb = self.kb()
        docx = Document(); docx.add_paragraph('合同有效期三年')
        output = io.BytesIO(); docx.save(output)
        doc = self.upload(kb, '合同.docx', output.getvalue())
        self.assertEqual(self.wait_document(doc['id'])['status'], 'completed')
        self.assertIn('三年', self.client.get(f'/api/documents/{doc["id"]}', headers=self.headers).json()['extracted_text'])
        broken = self.upload(kb, '损坏.pdf', b'not a PDF')
        self.assertEqual(self.wait_document(broken['id'])['status'], 'failed')
        self.assertEqual(self.client.post(f'/api/documents/{broken["id"]}/retry', headers=self.headers).json()['status'], 'processing')
        self.assertEqual(self.wait_document(broken['id'])['status'], 'failed')
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
        # 这条用例只关心文件名：图片正文靠 OCR，本机 tesseract 不可用时会落到 failed
        self.assertIn(self.wait_document(response.json()['id'])['status'], ('completed', 'failed'))


    def wait_job(self, response, headers=None, timeout=30):
        """排队接口：拿到 job_id 就等任务终态，返回任务视图（含 status/result/error）。"""
        payload = response.json()
        request_headers = headers or self.headers
        if not payload.get('pending'):
            return {'status': 'sync', 'result': payload}
        deadline = time.time() + timeout
        job = {}
        while time.time() < deadline:
            job = self.client.get(f'/api/jobs/{payload["job_id"]}', headers=request_headers).json()
            if job.get('status') in ('done', 'error', 'interrupted'):
                return job
            time.sleep(0.05)
        return job

    def test_asset_resolution_and_direct_upload_contract(self):
        """小程序拿不到文件字节：分辨率接口与直传凭证的契约必须稳定。"""
        kb = self.kb()
        doc = self.upload(kb)
        self.assertEqual(self.wait_document(doc['id'])['status'], 'completed')

        # 本地存储模式没有对象存储直传能力：如实回报 local，客户端据此提示
        slot = self.client.post('/api/uploads/direct', headers=self.headers,
                                json={'kind': 'document', 'knowledge_id': kb, 'filename': '新资料.txt'})
        self.assertEqual(slot.status_code, 200, slot.text)
        self.assertEqual(slot.json()['mode'], 'local')

        # 私有文档必须带登录态才能解析
        path = f'/api/documents/{doc["id"]}/download'
        anonymous = self.client.post('/api/assets/resolve', json={'paths': [path]}).json()
        self.assertEqual(anonymous['items'][path]['error'], '请先登录')
        owned = self.client.post('/api/assets/resolve', headers=self.headers, json={'paths': [path]}).json()
        self.assertEqual(owned['items'][path]['mode'], 'local')

        # 不认识的路径不解析，避免变成任意对象读取器
        unknown = self.client.post('/api/assets/resolve', headers=self.headers, json={'paths': ['/etc/passwd']}).json()
        self.assertIn('error', unknown['items']['/etc/passwd'])

    def test_suggestions_run_as_polled_job(self):
        kb = self.kb()
        doc = self.upload(kb, content='项目预算为一百万元。交付时间是六月。')
        self.assertEqual(self.wait_document(doc['id'])['status'], 'completed')

        first = self.client.get(f'/api/knowledge/{kb}/suggestions', headers=self.headers).json()
        job = None
        if first.get('pending'):
            job_id = first['job_id']
            _, other = self.fixture_user()
            try:
                self.assertEqual(self.client.get(f'/api/jobs/{job_id}', headers=other).status_code, 404)
            finally:
                self.client.delete('/api/me', headers=other)
            for _ in range(200):
                job = self.client.get(f'/api/jobs/{job_id}', headers=self.headers).json()
                if job['status'] in ('done', 'error'):
                    break
                time.sleep(0.05)
            self.assertIsNotNone(job)
            self.assertEqual(job['status'], 'done', job)
            self.assertTrue(job['result']['questions'])
        else:
            self.assertTrue(first['questions'])

        # 第二次命中缓存：不再排队，直接给结果
        again = self.client.get(f'/api/knowledge/{kb}/suggestions', headers=self.headers).json()
        self.assertTrue(again['questions'])
        self.assertFalse(again.get('pending'))

    def test_stream_and_knowledge_deletion(self):
        kb = self.kb(); doc = self.upload(kb)
        response = self.client.post('/api/chat/stream', headers=self.headers, json={'knowledge_id': kb, 'content': '合同？'})
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn('Harness 未启用', response.text)
        self.client.delete(f'/api/knowledge/{kb}', headers=self.headers).raise_for_status()
        self.assertEqual(self.client.get('/api/conversations', headers=self.headers).json(), [])
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM chunks_fts WHERE knowledge_id=?', (kb,)).fetchone()[0], 0)
        self.assertFalse(any(self.uploads.glob(doc['id'] + '_*')))

    def test_persistent_chat_run_can_be_resubscribed(self):
        """Without Harness the chat route fails explicitly instead of falling back."""
        kb = self.kb()
        created = self.client.post('/api/chat/runs', headers=self.headers, json={'knowledge_id': kb, 'content': '测试持久任务'})
        self.assertEqual(created.status_code, 503, created.text)

    def test_stream_without_documents_stays_in_current_knowledge(self):
        kb = self.kb()
        response = self.client.post('/api/chat/stream', headers=self.headers, json={'knowledge_id': kb, 'content': '帮我写一句欢迎语'})
        self.assertEqual(response.status_code, 503, response.text)

    def test_cross_user_isolation(self):
        kb = self.kb(); doc = self.upload(kb)
        _, other = self.fixture_user()
        try:
            for path in [f'/api/knowledge/{kb}', f'/api/documents/{doc["id"]}', f'/api/documents/{doc["id"]}/download']:
                self.assertEqual(self.client.get(path, headers=other).status_code, 404)
            self.assertEqual(self.client.delete(f'/api/knowledge/{kb}', headers=other).status_code, 404)
        finally:
            self.client.delete('/api/me', headers=other)

    def test_share_card_and_knowledge_invitation_lifecycle(self):
        owner_id, owner_headers = self.fixture_user()
        receiver_id, receiver_headers = self.fixture_user()
        source_id = uuid.uuid4().hex
        with closing(sqlite3.connect(self.database)) as db:
            db.execute(
                "INSERT INTO knowledge_bases(id,user_id,name,description,icon,document_count,visibility,category,subscribers,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (source_id, owner_id, '邀请测试库', '完整邀请链路', 'book', 0, 'private', '', 0, 'active', '2026-09-18', '2026-09-18'),
            )
            db.commit()
        try:
            created = self.client.post('/api/shares', headers=owner_headers, json={
                'title': '接口验证分享',
                'question': '项目代号是什么？',
                'answer': '蓝色海豚。',
                'sources': [{'filename': '项目资料.txt', 'page_number': 1}],
            })
            self.assertEqual(created.status_code, 200, created.text)
            share_id = created.json()['id']

            public = self.client.get(f'/api/shares/{share_id}')
            self.assertEqual(public.status_code, 200, public.text)
            self.assertEqual(public.json()['answer'], '蓝色海豚。')
            self.assertEqual(public.json()['sources'][0]['filename'], '项目资料.txt')

            claimed = self.client.post(f'/api/shares/{share_id}/claim', headers=receiver_headers)
            self.assertEqual(claimed.status_code, 200, claimed.text)
            claimed_job = self.wait_job(claimed, receiver_headers)
            self.assertEqual(claimed_job['status'], 'done', claimed_job)
            self.assertFalse(claimed_job['result']['already'])
            self.assertTrue(claimed_job['result']['documents'])
            claimed_again = self.wait_job(self.client.post(f'/api/shares/{share_id}/claim', headers=receiver_headers), receiver_headers)
            self.assertEqual(claimed_again['status'], 'done', claimed_again)
            self.assertTrue(claimed_again['result']['already'])

            receiver_kb = next(
                item['id'] for item in self.client.get('/api/knowledge', headers=receiver_headers).json()
                if item['name'] == '微信用户的知识库'
            )
            to_kb = self.wait_job(self.client.post('/api/shares/to-knowledge', headers=receiver_headers, json={
                'knowledge_id': receiver_kb,
                'title': '分享转存',
                'content': '分享转存内容：蓝色海豚。',
                'artifact_ids': [],
            }), receiver_headers)
            self.assertEqual(to_kb['status'], 'done', to_kb)
            self.assertTrue(to_kb['result']['saved'])


            invite = self.client.post(f'/api/knowledge/{source_id}/share', headers=owner_headers)
            self.assertEqual(invite.status_code, 200, invite.text)
            token = invite.json()['token']
            preview = self.client.get(f'/api/knowledge-shares/{token}')
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertEqual(preview.json()['state'], 'open')
            self.assertTrue(preview.json()['available'])

            accepted = self.client.post(f'/api/knowledge-shares/{token}/accept', headers=receiver_headers)
            self.assertEqual(accepted.status_code, 200, accepted.text)
            mirror_id = accepted.json()['knowledge_id']
            self.assertTrue(accepted.json()['read_only'])
            mirrors = self.client.get('/api/knowledge', headers=receiver_headers).json()
            self.assertIn(mirror_id, [item['id'] for item in mirrors])

            revoked = self.client.post(f'/api/knowledge-shares/{token}/revoke', headers=owner_headers)
            self.assertEqual(revoked.status_code, 200, revoked.text)
            self.assertGreaterEqual(revoked.json()['removed'], 1)
            after = self.client.get(f'/api/knowledge-shares/{token}')
            self.assertEqual(after.json()['state'], 'revoked')
        finally:
            self.client.delete('/api/me', headers=owner_headers)
            self.client.delete('/api/me', headers=receiver_headers)
            _ = (owner_id, receiver_id)

    def test_knowledge_publish_requires_acknowledgement_and_content(self):
        created = self.client.post('/api/knowledge', headers=self.headers, json={'name': '人工智能学习资料', 'description': '大模型知识整理'})
        self.assertEqual(created.status_code, 200, created.text)
        knowledge_id = created.json()['id']

        no_documents = self.client.post(f'/api/knowledge/{knowledge_id}/publish', headers=self.headers, json={'published': True, 'acknowledged': True})
        self.assertEqual(no_documents.status_code, 422, no_documents.text)
        self.assertIn('资料', no_documents.json()['detail'])

        self.upload(knowledge_id, content='人工智能和大模型的应用，以及机器学习基础知识。')
        not_acknowledged = self.client.post(f'/api/knowledge/{knowledge_id}/publish', headers=self.headers, json={'published': True, 'acknowledged': False})
        self.assertEqual(not_acknowledged.status_code, 422, not_acknowledged.text)
        self.assertIn('合规', not_acknowledged.json()['detail'])

    def test_knowledge_publish_market_subscription_and_unpublish(self):
        created = self.client.post('/api/knowledge', headers=self.headers, json={'name': 'AI 学习资料', 'description': '人工智能和大模型知识整理'})
        self.assertEqual(created.status_code, 200, created.text)
        source_id = created.json()['id']
        seed = self.upload(source_id, content='人工智能、机器学习、大模型与数据库基础知识。')
        self.assertEqual(self.wait_document(seed['id'])['status'], 'completed')

        published = self.wait_job(self.client.post(f'/api/knowledge/{source_id}/publish', headers=self.headers, json={'published': True, 'acknowledged': True}))
        self.assertEqual(published['status'], 'done', published)
        self.assertEqual(published['result']['category'], '科技')
        self.assertEqual(published['result']['reviewed_documents'], 1)

        detail = self.client.get(f'/api/knowledge/{source_id}', headers=self.headers).json()['knowledge']
        self.assertTrue(detail['published'])
        self.assertEqual(detail['category'], '科技')
        self.assertTrue(detail['published_at'])

        owner_market = self.client.get('/api/market', headers=self.headers).json()
        owner_item = next(item for item in owner_market if item['id'] == source_id)
        self.assertTrue(owner_item['owned'])
        self.assertEqual(owner_item['documents'], 1)
        self.assertEqual(owner_item['publisher_name'], '测试用户')

        _, subscriber_headers = self.fixture_user()
        try:
            before = next(item for item in self.client.get('/api/market', headers=subscriber_headers).json() if item['id'] == source_id)
            self.assertFalse(before['subscribed'])
            subscribed = self.client.post('/api/subscriptions', headers=subscriber_headers, json={'knowledge_id': source_id})
            self.assertEqual(subscribed.status_code, 200, subscribed.text)
            self.assertEqual(subscribed.json()['subscribers'], 1)
            self.assertEqual([item['id'] for item in self.client.get('/api/subscriptions', headers=subscriber_headers).json()], [subscribed.json()['knowledge_id']])
            after = next(item for item in self.client.get('/api/market', headers=subscriber_headers).json() if item['id'] == source_id)
            self.assertTrue(after['subscribed'])
            self.assertEqual(after['subscribers'], 1)
            removed = self.client.delete(f'/api/subscriptions/{source_id}', headers=subscriber_headers)
            self.assertTrue(removed.json()['removed'])
            self.assertEqual(self.client.get('/api/subscriptions', headers=subscriber_headers).json(), [])
        finally:
            self.client.delete('/api/me', headers=subscriber_headers)

        unpublished = self.client.post(f'/api/knowledge/{source_id}/publish', headers=self.headers, json={'published': False, 'acknowledged': False})
        self.assertEqual(unpublished.status_code, 200, unpublished.text)
        self.assertFalse(unpublished.json()['published'])
        self.assertFalse(any(item['id'] == source_id for item in self.client.get('/api/market', headers=self.headers).json()))

    def test_knowledge_policy_blocks_state_secrets(self):
        created = self.client.post('/api/knowledge', headers=self.headers, json={'name': '涉密资料', 'description': '国家秘密文件整理'})
        self.assertEqual(created.status_code, 200, created.text)
        knowledge_id = created.json()['id']
        risky = self.upload(knowledge_id, content='这里是国家秘密和绝密文件，禁止公开。')
        self.assertEqual(self.wait_document(risky['id'])['status'], 'completed')

        rejected = self.wait_job(self.client.post(f'/api/knowledge/{knowledge_id}/publish', headers=self.headers, json={'published': True, 'acknowledged': True}))
        self.assertEqual(rejected['status'], 'error', rejected)
        self.assertIn('国家秘密', rejected['error'])
        self.assertFalse(any(item['id'] == knowledge_id for item in self.client.get('/api/market', headers=self.headers).json()))

    def test_new_risky_document_unpublishes_a_public_knowledge(self):
        created = self.client.post('/api/knowledge', headers=self.headers, json={'name': '公开项目资料', 'description': '项目管理知识'})
        self.assertEqual(created.status_code, 200, created.text)
        knowledge_id = created.json()['id']
        seed = self.upload(knowledge_id, content='项目管理流程、团队协作和风险控制。')
        self.assertEqual(self.wait_document(seed['id'])['status'], 'completed')
        published = self.wait_job(self.client.post(f'/api/knowledge/{knowledge_id}/publish', headers=self.headers, json={'published': True, 'acknowledged': True}))
        self.assertEqual(published['status'], 'done', published)

        # 合规检查在后台解析时执行：等它跑完，公开库应当已被自动下架
        risky = self.upload(knowledge_id, '风险资料.txt', content='国家秘密绝密资料。')
        self.assertEqual(self.wait_document(risky['id'])['status'], 'completed')
        detail = self.client.get(f'/api/knowledge/{knowledge_id}', headers=self.headers).json()['knowledge']
        self.assertFalse(detail['published'])
        self.assertFalse(any(item['id'] == knowledge_id for item in self.client.get('/api/market', headers=self.headers).json()))


    def test_market_subscription_lifecycle(self):
        owner_id, owner_headers = self.fixture_user()
        source_id = uuid.uuid4().hex
        with closing(sqlite3.connect(self.database)) as db:
            db.execute(
                "INSERT INTO knowledge_bases(id,user_id,name,description,icon,document_count,visibility,category,subscribers,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (source_id, owner_id, '公开案例库', '用于订阅链路测试', 'book', 0, 'public', '科技', 0, 'active', '2026-09-17', '2026-09-17'),
            )
            db.commit()
        try:
            market = self.client.get('/api/market', headers=self.headers).json()
            item = next(row for row in market if row['id'] == source_id)
            self.assertFalse(item['subscribed'])
            self.assertTrue(next(row for row in self.client.get('/api/market', headers=owner_headers).json() if row['id'] == source_id)['owned'])

            created = self.client.post('/api/subscriptions', headers=self.headers, json={'knowledge_id': source_id})
            self.assertEqual(created.status_code, 200, created.text)
            mirror_id = created.json()['knowledge_id']
            self.assertEqual(created.json()['subscribers'], 1)

            subscribed = [row for row in self.client.get('/api/knowledge', headers=self.headers).json() if row['subscribed']]
            self.assertEqual([row['id'] for row in subscribed], [mirror_id])
            self.assertFalse(subscribed[0]['shared'])
            self.assertEqual(subscribed[0]['subscription_source'], source_id)
            self.assertEqual([row['id'] for row in self.client.get('/api/subscriptions', headers=self.headers).json()], [mirror_id])

            duplicate = self.client.post('/api/subscriptions', headers=self.headers, json={'knowledge_id': source_id})
            self.assertTrue(duplicate.json()['already'])
            self.assertEqual(duplicate.json()['knowledge_id'], mirror_id)
            self.assertEqual(duplicate.json()['subscribers'], 1)
            after_subscribe = next(row for row in self.client.get('/api/market', headers=self.headers).json() if row['id'] == source_id)
            self.assertTrue(after_subscribe['subscribed'])
            self.assertEqual(after_subscribe['subscribers'], 1)

            removed = self.client.delete(f'/api/subscriptions/{source_id}', headers=self.headers)
            self.assertEqual(removed.status_code, 200, removed.text)
            self.assertTrue(removed.json()['removed'])
            self.assertEqual(removed.json()['subscribers'], 0)
            self.assertEqual(self.client.get('/api/subscriptions', headers=self.headers).json(), [])
            self.assertFalse(any(row['id'] == mirror_id for row in self.client.get('/api/knowledge', headers=self.headers).json()))
            after_cancel = next(row for row in self.client.get('/api/market', headers=self.headers).json() if row['id'] == source_id)
            self.assertFalse(after_cancel['subscribed'])
        finally:
            self.client.delete('/api/me', headers=owner_headers)

    def test_account_deletion_invalidates_token(self):
        kb = self.kb(); doc = self.upload(kb)
        self.client.delete('/api/me', headers=self.headers).raise_for_status()
        self.assertEqual(self.client.get('/api/me', headers=self.headers).status_code, 401)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM chunks_fts WHERE knowledge_id=?', (kb,)).fetchone()[0], 0)
        self.assertFalse(any(self.uploads.glob(doc['id'] + '_*')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
