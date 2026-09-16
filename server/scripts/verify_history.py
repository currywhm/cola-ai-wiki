"""验证「历史对话」是否真正持久化，以及用户级 / 知识库级 / 文件夹级隔离是否成立。

覆盖四件事：
1. 真发一轮问答 -> 会话与消息落库（持久化）
2. /api/conversations 按 knowledge_id + folder_id 收窄（列表正确）
3. 另一个用户看不到别人的会话（用户级隔离）
4. 另一个用户带着别人的 conversation_id 提问会被拒（越权拦截）

用法：python3 scripts/verify_history.py
"""
from __future__ import annotations

import asyncio
import json
import sys

sys.path.insert(0, '/Users/mac/WeChatProjects/zhi-reader-api')

import httpx  # noqa: E402
from app.db import connect, fetchall, fetchone  # noqa: E402
from app.security import create_token  # noqa: E402

BASE = 'http://127.0.0.1:8765'


async def auth_headers(user_id: str) -> dict:
    db = await connect()
    row = await fetchone(db, 'SELECT token_version FROM users WHERE id=?', (user_id,))
    await db.close()
    version = int(row['token_version'] or 0) if row else 0
    return {'Authorization': f"Bearer {create_token(user_id, version)}"}


async def two_users() -> tuple[str, str]:
    db = await connect()
    rows = await fetchall(db, "SELECT id FROM users WHERE status='active' ORDER BY created_at LIMIT 2")
    await db.close()
    if len(rows) < 2:
        raise SystemExit('至少需要两个用户才能验证隔离')
    return rows[0]['id'], rows[1]['id']


async def main() -> None:
    user_a, user_b = await two_users()
    head_a = await auth_headers(user_a)
    head_b = await auth_headers(user_b)

    async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=10)) as client:
        kb = (await client.get(f'{BASE}/api/knowledge', headers=head_a)).json()[0]
        folders = (await client.get(f'{BASE}/api/knowledge/{kb["id"]}/folders', headers=head_a)).json()
        if not folders:
            folders = [(await client.post(f'{BASE}/api/knowledge/{kb["id"]}/folders', headers=head_a, json={'name': '历史验证夹'})).json()]
        folder = folders[0]
        print(f'用户A={user_a[:8]}… 知识库={kb["name"]} 文件夹={folder["name"]}')

        # 1) 真发一轮问答，让会话与消息落库
        body = {'content': '用一句话说明这个文件夹里有什么。', 'mode': 'knowledge', 'thinking': 'quick',
                'knowledge_id': kb['id'], 'folder_id': folder['id']}
        conversation_id = ''
        async with client.stream('POST', f'{BASE}/api/chat/stream', json=body,
                                 headers={**head_a, 'content-type': 'application/json'}) as resp:
            buffer = ''
            async for chunk in resp.aiter_text():
                buffer += chunk
                while '\n\n' in buffer:
                    raw, buffer = buffer.split('\n\n', 1)
                    if not raw.startswith('data: '):
                        continue
                    data = json.loads(raw[6:])
                    if data.get('type') == 'meta':
                        conversation_id = data['conversation_id']
        print(f'1) 本轮会话 id={conversation_id[:12]}…')

        # 2) 列表：按知识库 + 文件夹收窄
        listed = (await client.get(f'{BASE}/api/conversations', headers=head_a,
                                   params={'knowledge_id': kb['id'], 'folder_id': folder['id']})).json()
        ids = [item['id'] for item in listed]
        print(f'2) 文件夹内会话 {len(listed)} 条，命中本轮：{conversation_id in ids}')

        detail = (await client.get(f'{BASE}/api/conversations/{conversation_id}', headers=head_a)).json()
        roles = [m['role'] for m in detail]
        print(f'3) 消息持久化 {len(detail)} 条，角色序列={roles}')

        # 3) 用户级隔离：B 的列表不含 A 的会话
        listed_b = (await client.get(f'{BASE}/api/conversations', headers=head_b)).json()
        leaked = [item['id'] for item in listed_b if item['id'] == conversation_id]
        print(f'4) 用户B 列表 {len(listed_b)} 条，泄漏 A 的会话：{bool(leaked)}')

        # 4) 越权拦截：B 用 A 的 conversation_id 提问
        resp_b = await client.post(f'{BASE}/api/chat/stream',
                                   json={'content': '继续', 'mode': 'knowledge', 'conversation_id': conversation_id},
                                   headers={**head_b, 'content-type': 'application/json'})
        print(f'5) 用户B 续写 A 的会话 -> HTTP {resp_b.status_code}（应为 404）')

        # 5) 库内直查再确认一次落库
        db = await connect()
        row = await fetchone(db, 'SELECT id,user_id,knowledge_id,folder_id,title FROM conversations WHERE id=?', (conversation_id,))
        count = await fetchone(db, 'SELECT COUNT(*) AS n FROM messages WHERE conversation_id=?', (conversation_id,))
        await db.close()
        print(f'6) 库内记录：user={row["user_id"][:8]}… kb={row["knowledge_id"][:8]}… folder={str(row["folder_id"])[:8]}… 标题={row["title"]} 消息数={count["n"]}')


asyncio.run(main())
