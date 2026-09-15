import json
import uuid
from collections.abc import AsyncIterator
import httpx
from ..config import settings
from .harness import configured as harness_configured, stream_answer as harness_stream_answer


def _compose_history_question(history: list[dict], question: str) -> str:
    """把记忆摘要与近期对话内联进 harness 提示词（参考 harness 会话事件日志的上下文形态）。

    后端 DB 是记忆的唯一真相源：harness/直连双通道看到完全一致的上下文，
    避免 Agent 轮次失败回落直连后记忆断裂。
    """
    labels = {'user': '用户', 'assistant': '助手', 'system': '背景'}
    lines = [f"{labels.get(m.get('role'), '背景')}：{m.get('content', '')}" for m in history if m.get('content')]
    if not lines:
        return question
    return (
        '以下是本会话的历史上下文（含记忆摘要与近期对话，仅供连贯理解，不要在回答中复述）：\n'
        + '\n\n'.join(lines)
        + '\n\n请结合以上上下文，回答用户的最新问题：\n'
        + question
    )


def provider_config(model: str) -> tuple[str, str, str] | None:
    # deepseek 系模型优先走 DeepSeek 官方直连（绕过 airouter 的上游过载/断流）
    if model.startswith("deepseek") and settings.deepseek_api_key:
        return settings.deepseek_base_url, settings.deepseek_api_key, model
    configured = {item['value'] for item in settings.chat_model_list}
    if model in configured and settings.openai_api_key:
        return settings.openai_base_url, settings.openai_api_key, model
    if model.startswith("MiniMax") and settings.minimax_api_key:
        return settings.minimax_base_url, settings.minimax_api_key, settings.minimax_model
    if (model.startswith("OpenAI") or model.startswith("gpt") or model.startswith("o1") or model.startswith("o3")) and settings.openai_api_key:
        return settings.openai_base_url, settings.openai_api_key, settings.openai_model
    # The client uses these stable aliases for its quick/deep model switch.
    # Keep the alias as the actual upstream model instead of silently falling
    # back to the configured default, so "深度" really uses the reasoner.
    if model in {"deepseek-chat", "deepseek-reasoner"} and settings.deepseek_api_key:
        return settings.deepseek_base_url, settings.deepseek_api_key, model
    if model.startswith("DeepSeek") and settings.deepseek_api_key:
        return settings.deepseek_base_url, settings.deepseek_api_key, settings.deepseek_model
    if settings.deepseek_api_key:
        return settings.deepseek_base_url, settings.deepseek_api_key, settings.deepseek_model
    if settings.openai_api_key:
        return settings.openai_base_url, settings.openai_api_key, settings.openai_model
    return None


async def stream_answer(messages: list[dict], model: str, session_id: str = '', thinking: str = 'quick') -> AsyncIterator[dict]:
    # thinking=quick 走 sdk-minimal 精简组合（快），deep 走完整 sdk profile（工具全）；
    # 两条路都在 harness 内，失败时非严格模式回落直连通道。
    # 产出 {'kind':'text'|'progress','text':...}：progress 为 agent 过程标签。
    if harness_configured():
        produced = False
        try:
            system = messages[0].get('content', '') if messages else ''
            question = _compose_history_question(messages[1:-1], messages[-1].get('content', '') if messages else '')
            # harness 会话每轮新建（上下文由 DB 注入）：避免跨轮复用引发的
            # "session already exists" 冲突，且重试/回落后记忆不丢失
            turn_session = f'{session_id}-{uuid.uuid4().hex[:8]}' if session_id else ''
            async for event in harness_stream_answer(question, system, turn_session, model, thinking):
                if event.get('kind') == 'text' and event.get('text'):
                    produced = True
                yield event
        except (RuntimeError, OSError) as exc:
            if settings.harness_strict:
                raise RuntimeError(f"Harness 问答链路失败：{exc}") from exc
            # 非严格模式：harness 失败回落直连通道，保证链路可靠
            print(f'[llm] harness 失败，回落直连通道：{exc}', flush=True)
        if produced:
            return
        # harness 正常结束但零产出（网关断流等），同样回落直连，避免空气泡
        print('[llm] harness 零产出，回落直连通道', flush=True)
        yield {'kind': 'progress', 'text': '网络波动，正在切换应答通道…'}
    config = provider_config(model)
    if not config:
        fallback = "当前服务已完成资料检索，但智能模型尚未完成配置。请联系管理员启用问答模型后重试。\n\n我已保留参考出处，方便你继续核对原文。"
        for piece in [fallback[i:i + 18] for i in range(0, len(fallback), 18)]:
            yield {'kind': 'text', 'text': piece}
        return
    base_url, api_key, provider_model = config
    payload = {"model": provider_model, "messages": messages, "stream": True, "temperature": 0.2}
    endpoint = f"{base_url.rstrip('/')}/chat/completions" if base_url.rstrip('/').endswith('/v1') else f"{base_url.rstrip('/')}/v1/chat/completions"
    # 网关约 2-3 成概率断流导致零产出：无内容流出时整轮重试（最多 3 次）；
    # 一旦有任何内容流出即不再重试，避免重复文本
    for direct_attempt in range(3):
        got = False
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as client:
                async with client.stream("POST", endpoint, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        upstream_err = data.get('error')
                        if upstream_err:
                            # 网关流内错误（上游过载等）：抛出触发整轮重试并留下真实原因
                            raise httpx.HTTPError(f"上游模型错误：{upstream_err.get('message') or upstream_err}")
                        choices = data.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta", {}).get("content")
                        if delta:
                            got = True
                            yield {'kind': 'text', 'text': delta}
        except (httpx.HTTPError, OSError) as exc:
            print(f'[llm] 直连通道第 {direct_attempt + 1} 次异常：{exc}', flush=True)
        if got:
            return
        if direct_attempt + 1 < 3:
            print(f'[llm] 直连通道零产出，重试（{direct_attempt + 2}/3）', flush=True)
            yield {'kind': 'progress', 'text': f'网络波动，正在重试…（{direct_attempt + 2}/3）'}
    # 三次均零产出：返回空，由 main.py 空回答守卫向前端发 error 事件
