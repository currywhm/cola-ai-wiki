import json
import re
from pathlib import Path
from typing import Any

import httpx

from ..config import settings
from ..db import connect, fetchone
from .llm import provider_config


def _sentences(text: str) -> list[str]:
    normalized = re.sub(r'\s+', ' ', text).strip()
    return [item.strip() for item in re.split(r'(?<=[。！？.!?])\s*', normalized) if item.strip()]


def local_organize(filename: str, text: str) -> dict[str, Any]:
    """Useful offline baseline: deterministic, source-preserving metadata."""
    stem = Path(filename).stem.strip() or '未命名文档'
    sentences = _sentences(text)
    summary = (sentences[0] if sentences else text.strip())[:240] or '文档暂无可提取文本。'
    points = [item[:160] for item in sentences[:4]] or [summary]
    words = re.findall(r'[\u4e00-\u9fff]{2,8}|[A-Za-z][A-Za-z0-9_-]{2,20}', text)
    frequency: dict[str, int] = {}
    for word in words:
        frequency[word] = frequency.get(word, 0) + 1
    tags = [word for word, _ in sorted(frequency.items(), key=lambda pair: (-pair[1], pair[0]))[:6]]
    return {'title': stem[:80], 'summary': summary, 'tags': tags, 'key_points': points, 'method': 'local'}


def _parse_json(content: str) -> dict[str, Any] | None:
    candidate = content.strip()
    if candidate.startswith('```'):
        candidate = re.sub(r'^```(?:json)?\s*|\s*```$', '', candidate, flags=re.I | re.S).strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', candidate, flags=re.S)
        if not match:
            return None
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict):
        return None
    return value


async def _llm_organize(filename: str, text: str) -> dict[str, Any] | None:
    config = provider_config('organize') or provider_config('MiniMax-organize')
    if not config:
        return None
    base_url, api_key, model = config
    source = text[:14000]
    prompt = (
        '你是个人知识库整理器。请根据文档原文生成结构化元数据，只能使用原文事实，不要臆测。'
        '严格只返回 JSON，不要 Markdown。字段必须是 title(string), summary(string,不超过240字), '
        'tags(string数组,最多6个), key_points(string数组,最多4个)。\n'
        f'文件名：{filename}\n原文：\n{source}'
    )
    payload = {'model': model, 'messages': [{'role': 'system', 'content': '你负责可靠的文档整理。'}, {'role': 'user', 'content': prompt}], 'temperature': 0.1, 'stream': False}
    endpoint = f"{base_url.rstrip('/')}/chat/completions" if base_url.rstrip('/').endswith('/v1') else f"{base_url.rstrip('/')}/v1/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=10)) as client:
            response = await client.post(endpoint, headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, json=payload)
            response.raise_for_status()
            content = response.json().get('choices', [{}])[0].get('message', {}).get('content', '')
        value = _parse_json(content)
        if not value:
            return None
        return {
            'title': str(value.get('title') or Path(filename).stem)[:80],
            'summary': str(value.get('summary') or '')[:240],
            'tags': [str(item)[:24] for item in value.get('tags', []) if str(item).strip()][:6],
            'key_points': [str(item)[:160] for item in value.get('key_points', []) if str(item).strip()][:4],
            'method': 'llm',
        }
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
        return None


async def organize_document(document_id: str) -> None:
    db = await connect()
    row = await fetchone(db, 'SELECT id,filename,extracted_text,status FROM documents WHERE id=? AND status!=\'deleted\'', (document_id,))
    if not row:
        await db.close()
        return
    await db.execute("UPDATE documents SET organize_status='processing',organize_error='',updated_at=? WHERE id=?", (__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(), document_id))
    await db.commit(); await db.close()
    result = await _llm_organize(row['filename'], row['extracted_text'])
    if not result:
        result = local_organize(row['filename'], row['extracted_text'])
    timestamp = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
    db = await connect()
    await db.execute("UPDATE documents SET organized_title=?,summary=?,tags_json=?,key_points_json=?,organize_status='completed',organize_method=?,organize_error='',organized_at=?,updated_at=? WHERE id=?", (result['title'], result['summary'], json.dumps(result['tags'], ensure_ascii=False), json.dumps(result['key_points'], ensure_ascii=False), result['method'], timestamp, timestamp, document_id))
    await db.commit(); await db.close()
