"""Model entry points.

Chat and task execution have one path: the official DeepSeek Harness Python
SDK. ``provider_config`` remains only for non-agent document metadata helpers
and must never be used as a chat fallback.
"""

from collections.abc import AsyncIterator

from ..config import settings
from .harness import configured as harness_configured, stream_answer as harness_stream_answer


def provider_config(model: str) -> tuple[str, str, str] | None:
    generic_key = settings.llm_api_key.strip()
    if generic_key:
        requested = str(model or '').strip()
        upstream_model = settings.llm_model.strip()
        if not upstream_model and requested not in {'organize', 'MiniMax-organize'}:
            upstream_model = requested
        upstream_model = upstream_model or settings.deepseek_model
        return settings.llm_base_url.strip() or settings.deepseek_base_url, generic_key, upstream_model
    if model.startswith("deepseek") and settings.deepseek_api_key:
        return settings.deepseek_base_url, settings.deepseek_api_key, model
    configured = {item['value'] for item in settings.chat_model_list}
    if model in configured and settings.openai_api_key:
        return settings.openai_base_url, settings.openai_api_key, model
    if model.startswith("MiniMax") and settings.minimax_api_key:
        return settings.minimax_base_url, settings.minimax_api_key, settings.minimax_model
    if (
        model.startswith("OpenAI")
        or model.startswith("gpt")
        or model.startswith("o1")
        or model.startswith("o3")
    ) and settings.openai_api_key:
        return settings.openai_base_url, settings.openai_api_key, settings.openai_model
    if model in {"deepseek-chat", "deepseek-reasoner"} and settings.deepseek_api_key:
        return settings.deepseek_base_url, settings.deepseek_api_key, model
    if model.startswith("DeepSeek") and settings.deepseek_api_key:
        return settings.deepseek_base_url, settings.deepseek_api_key, settings.deepseek_model
    if settings.deepseek_api_key:
        return settings.deepseek_base_url, settings.deepseek_api_key, settings.deepseek_model
    if settings.openai_api_key:
        return settings.openai_base_url, settings.openai_api_key, settings.openai_model
    return None


async def stream_answer(
    question: str,
    context: str = '',
    model: str = '',
    session_id: str = '',
    thinking: str = 'quick',
    skills: list[dict] | None = None,
    mode: str = 'knowledge',
    user_id: str = '',
    history: list[dict] | None = None,
    plan: bool = False,
) -> AsyncIterator[dict]:
    """Stream one official Harness turn.

    There is deliberately no direct ``/chat/completions`` fallback here.
    Session persistence, compaction, retries and tool iteration belong to
    Harness; duplicating them in the backend would fork the execution model.
    """
    if not harness_configured():
        raise RuntimeError('Harness 未启用，聊天接口不会回落到直连模型')
    async for event in harness_stream_answer(
        question=question,
        context=context,
        session_id=session_id,
        model=model,
        thinking=thinking,
        skills=skills,
        mode=mode,
        user_id=user_id,
        history=history,
        plan=plan,
    ):
        yield event
