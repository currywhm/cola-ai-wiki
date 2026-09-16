"""End-to-end SSE check for /api/chat/stream: prints trace + delta timings."""
from __future__ import annotations

import asyncio
import json
import sys
import time

sys.path.insert(0, '/Users/mac/WeChatProjects/zhi-reader-api')

import httpx  # noqa: E402
from app.db import connect, fetchall  # noqa: E402
from app.security import create_token  # noqa: E402

BASE = 'http://127.0.0.1:8765'


async def token_for_first_user() -> tuple[str, str]:
    db = await connect()
    rows = await fetchall(db, "SELECT id FROM users WHERE status='active' ORDER BY created_at LIMIT 1")
    await db.close()
    if not rows:
        raise SystemExit('没有可用用户')
    user_id = rows[0]['id']
    return user_id, create_token(user_id, 0)


async def main() -> None:
    user_id, token = await token_for_first_user()
    content = sys.argv[1] if len(sys.argv) > 1 else '这个知识库能做什么？用一句话说明。'
    mode = sys.argv[2] if len(sys.argv) > 2 else 'knowledge'
    skill = sys.argv[3] if len(sys.argv) > 3 else ''
    body = {'content': content, 'mode': mode, 'thinking': 'deep'}
    if skill:
        body['skill'] = skill
    t0 = time.time()
    answer = ''
    reason = ''
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as client:
        async with client.stream('POST', f'{BASE}/api/chat/stream', json=body,
                                 headers={'Authorization': f'Bearer {token}', 'content-type': 'application/json'}) as resp:
            print('status', resp.status_code, f'user={user_id[:8]}…', flush=True)
            buffer = ''
            async for chunk in resp.aiter_text():
                buffer += chunk
                while '\n\n' in buffer:
                    raw, buffer = buffer.split('\n\n', 1)
                    if not raw.startswith('data: '):
                        continue
                    data = json.loads(raw[6:])
                    kind = data.get('type')
                    dt = time.time() - t0
                    if kind == 'delta':
                        answer += data.get('content') or ''
                        if len(answer) <= 60 or dt > 0:
                            pass
                    elif kind == 'trace' and data.get('kind') == 'reason':
                        reason += data.get('text') or ''
                    elif kind == 'trace':
                        print(f'[{dt:6.1f}s] TRACE {data.get("kind"):7s} {data.get("state", ""):8s} {data.get("title", "")} {data.get("detail", "")}', flush=True)
                    else:
                        print(f'[{dt:6.1f}s] {kind} {str(data)[:120]}', flush=True)
    print(f'--- total {time.time()-t0:.1f}s, answer {len(answer)} chars, reason {len(reason)} chars ---')
    print('REASON:', reason[:260].replace(chr(10), ' '))
    print(answer[:800])


asyncio.run(main())
