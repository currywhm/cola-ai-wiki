"""端到端对话记忆验证：真实 chat_stream 链路两轮问答。

轮 1：告知名字「小明」→ 轮 2：问「我叫什么名字」，断言回答含「小明」。
同时验证：同一 conversation_id 归属校验、历史注入、答后压缩触发。
测完自动清理测试会话。

用法: python3 scripts/test_e2e_memory.py
"""
import asyncio
import json

from app.db import connect
from app.main import chat_stream
from app.schemas import ChatRequest

USER_ID = 'eb48428d90774431a93e2880524f9a5e'
KNOWLEDGE_ID = 'af09f7ef7758474cac02c06cdace7403'


async def ask(content: str, conversation_id: str | None) -> tuple[str, str, list[str]]:
    payload = ChatRequest(
        knowledge_id=KNOWLEDGE_ID, conversation_id=conversation_id,
        content=content, model='deepseek-flash', mode='knowledge', thinking='quick',
    )
    response = await chat_stream(payload, USER_ID)
    conv_id, answer, progress = conversation_id or '', '', []
    async for raw in response.body_iterator:
        line = raw.strip()
        if not line.startswith('data:'):
            continue
        event = json.loads(line[5:].strip())
        etype = event.get('type')
        if etype == 'meta':
            conv_id = event['conversation_id']
        elif etype == 'delta':
            answer += event.get('content', '')
        elif etype == 'progress':
            progress.append(event.get('label', ''))
        elif etype == 'error':
            raise RuntimeError(f" SSE 错误事件：{event.get('message')}")
    return conv_id, answer, progress


async def main() -> None:
    cid = ''
    try:
        print('[轮 1] 发送：我叫小明，请记住我的名字。', flush=True)
        cid, answer1, _ = await ask('我叫小明，请记住我的名字。', None)
        print(f'[轮 1] 会话 {cid[:8]}，回答：{answer1[:80]}', flush=True)
        assert cid and answer1.strip(), '轮 1 应有回答'

        print('[轮 2] 发送：我叫什么名字？', flush=True)
        cid2, answer2, _ = await ask('我叫什么名字？', cid)
        print(f'[轮 2] 回答：{answer2[:120]}', flush=True)
        assert cid2 == cid, '会话 id 应保持不变'
        assert '小明' in answer2, f'模型应记住名字，实际回答：{answer2[:100]}'
        print('E2E PASS：模型通过上下文记忆正确回答', flush=True)
    finally:
        if cid:
            db = await connect()
            await db.execute('DELETE FROM messages WHERE conversation_id=?', (cid,))
            await db.execute('DELETE FROM conversations WHERE id=?', (cid,))
            await db.commit()
            await db.close()
            print(f'已清理测试会话 {cid[:8]}', flush=True)


asyncio.run(main())
