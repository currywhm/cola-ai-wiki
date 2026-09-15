"""对话记忆链路验证：历史加载、最近 N 条切片、滚动压缩、历史内联组词。

用法: python3 scripts/test_memory.py
会直接操作 data/llmwiki.db 建一个临时会话，测完自动清理。
"""
import asyncio
import uuid

from app.db import connect, fetchone
from app.services.llm import _compose_history_question
from app.services.memory import load_history, maybe_compress, recent_context

USER_ID = '1a11dc0b7c69448b84117642b526ad8d'
KNOWLEDGE_ID = 'f300b807c9fc4cc5aaa89431242c0d58'
NOW = '2026-09-15T09:30:00+00:00'


async def main() -> None:
    db = await connect()
    cid = 'memtest-' + uuid.uuid4().hex[:12]
    try:
        await db.execute(
            "INSERT INTO conversations(id,user_id,knowledge_id,title,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (cid, USER_ID, KNOWLEDGE_ID, '记忆测试', NOW, NOW),
        )
        # 12 条消息（6 轮），内容故意拉长以触发压缩阈值
        for i in range(12):
            role = 'user' if i % 2 == 0 else 'assistant'
            content = f'第{i // 2 + 1}轮{"提问" if role == "user" else "回答"}：' + ('这是一段用于填充长度的测试内容。' * 80)
            await db.execute(
                "INSERT INTO messages(id,conversation_id,role,content,sources_json,created_at) VALUES(?,?,?,?,?,?)",
                (uuid.uuid4().hex, cid, role, content, '[]', f'2026-09-15T09:{30 + i:02d}:00+00:00'),
            )
        await db.commit()

        history = await load_history(db, cid)
        recent = recent_context(history)
        print(f'[1] 全量历史 {len(history)} 条；进入上下文的最近 {len(recent)} 条')
        assert len(history) == 12 and len(recent) == 8
        assert recent[-1]['content'].startswith('第6轮回答'), '末条应是最后一轮回答'

        # 压缩前摘要为空
        row = await fetchone(db, 'SELECT memory_summary FROM conversations WHERE id=?', (cid,))
        assert not row['memory_summary'], '压缩前摘要应为空'

        await maybe_compress(db, cid, 'deepseek-flash')
        row = await fetchone(db, 'SELECT memory_summary FROM conversations WHERE id=?', (cid,))
        summary = row['memory_summary'] or ''
        print(f'[2] 压缩后摘要 {len(summary)} 字：{summary[:120]}')
        assert summary, '压缩后摘要不应为空（stale 4 条 × 约 1.3k 字 > 3000 阈值，必触发）'

        # 历史内联组词
        composed = _compose_history_question(
            [{'role': 'system', 'content': '记忆摘要：用户叫小明'},
             {'role': 'user', 'content': '我叫小明'}, {'role': 'assistant', 'content': '已记住'},
             ], '我叫什么名字？')
        assert '记忆摘要' in composed and '用户：我叫小明' in composed and '助手：已记住' in composed
        assert composed.endswith('我叫什么名字？')
        print('[3] 历史内联组词 OK：')
        print('    ' + composed.replace('\n', '\n    ')[:220])

        # 无历史时原样返回
        assert _compose_history_question([], '你好') == '你好'
        print('[4] 无历史直通 OK')
        print('ALL PASS')
    finally:
        await db.execute('DELETE FROM messages WHERE conversation_id=?', (cid,))
        await db.execute('DELETE FROM conversations WHERE id=?', (cid,))
        await db.commit()
        await db.close()


asyncio.run(main())
