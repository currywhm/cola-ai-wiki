"""对话记忆：历史加载 + 滚动压缩（参考 deepseek-harness 的 session 持久化与 compaction）。

架构对齐 harness：
- messages 表 = 事件日志（全量持久化，供历史界面回放）
- conversations.memory_summary = 压缩态（compaction 产物）
- 每轮上下文 = memory_summary + 最近 N 条原文；旧轮次累计超阈值时，
  用 LLM 把「已有摘要 + 旧轮次」滚动合并成新摘要（对齐 harness 的 compaction）

隔离说明：所有查询只按 conversation_id 过滤——调用方（main.py）已先用
user_id + knowledge_id 校验会话归属，因此用户之间、知识库之间的记忆天然隔离。
"""
import aiosqlite
import httpx

from ..config import settings
from ..db import fetchall, fetchone
from .llm import provider_config

_COMPRESS_SYSTEM = (
    '你是对话压缩器。把「已有摘要」和「新增对话」合并压缩成一段连贯的中文记忆摘要，'
    '保留：用户的身份线索与偏好、明确提出的需求、关键事实与结论、未完成的待办事项；'
    '丢弃寒暄、客套与重复内容。只输出摘要本身，不要任何解释或前后缀。'
)


async def load_history(db: aiosqlite.Connection, conversation_id: str) -> list[dict]:
    """加载会话全部消息（升序）。调用方须已完成 user/knowledge 归属校验。"""
    rows = await fetchall(
        db,
        'SELECT role,content FROM messages WHERE conversation_id=? ORDER BY created_at ASC, rowid ASC',
        (conversation_id,),
    )
    return [{'role': r['role'], 'content': r['content']} for r in rows]


def recent_context(history: list[dict]) -> list[dict]:
    """取最近 N 条原文进入上下文；更早的轮次由 memory_summary 承载。"""
    return history[-settings.memory_recent_messages:]


async def maybe_compress(db: aiosqlite.Connection, conversation_id: str, model: str) -> None:
    """旧轮次（最近 N 条之前）累计超阈值时，滚动压缩进 memory_summary。

    失败（模型不可用等）跳过本轮：旧摘要保留，下轮问答后重试，
    上下文构建只取最近 N 条，历史不会无界进入模型。
    """
    history = await load_history(db, conversation_id)
    if len(history) <= settings.memory_recent_messages:
        return
    stale = history[:-settings.memory_recent_messages]
    if sum(len(m['content']) for m in stale) < settings.memory_compress_chars:
        return
    conv = await fetchone(db, 'SELECT memory_summary FROM conversations WHERE id=?', (conversation_id,))
    previous = (conv['memory_summary'] or '') if conv else ''
    summary = await _summarize(model, previous, stale)
    if not summary:
        return
    await db.execute('UPDATE conversations SET memory_summary=? WHERE id=?', (summary, conversation_id))
    await db.commit()
    print(f'[memory] 会话 {conversation_id[:8]} 记忆已压缩：{len(stale)} 条旧消息 → {len(summary)} 字摘要', flush=True)


async def _summarize(model: str, previous: str, stale: list[dict]) -> str:
    """非流式调用模型做滚动压缩；任何异常返回空串（跳过本轮压缩）。"""
    config = provider_config(model)
    if not config:
        return ''
    base_url, api_key, provider_model = config
    endpoint = f"{base_url.rstrip('/')}/chat/completions" if base_url.rstrip('/').endswith('/v1') else f"{base_url.rstrip('/')}/v1/chat/completions"
    labels = {'user': '用户', 'assistant': '助手'}
    # 旧轮次输入封顶 6000 字：更久远的信息已由 previous 摘要承载
    body = '\n'.join(f"{labels.get(m['role'], '助手')}：{m['content']}" for m in stale)[-6000:]
    payload = {
        'model': provider_model,
        'messages': [
            {'role': 'system', 'content': _COMPRESS_SYSTEM},
            {'role': 'user', 'content': f'已有摘要：{previous or "（无）"}\n\n新增对话：\n{body}'},
        ],
        'stream': False,
        'temperature': 0.1,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=10)) as client:
            resp = await client.post(endpoint, headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, OSError, ValueError) as exc:
        print(f'[memory] 压缩调用失败：{exc}', flush=True)
        return ''
    choices = data.get('choices') or []
    if not choices:
        return ''
    summary = ((choices[0].get('message') or {}).get('content') or '').strip()
    # 硬上限：防止模型输出失控撑爆后续上下文
    return summary[: settings.memory_summary_max_chars * 2]
