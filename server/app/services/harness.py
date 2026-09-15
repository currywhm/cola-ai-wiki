"""DeepSeek Harness bridge: agent turns with session persistence, tools and skills.

对齐 deepseek-harness 完整版核心链路：
- 多轮对话：session_id 复用（JSONL 事件日志自动持久化到 DSH_HOME）
- 智能体：profile 分流——快速= sdk-minimal（精简组合，无 web/skill/plan-mode，
  includeHarnessIdentity/RuntimeContext 关闭，prompt 小、启动快）；
  深度= sdk 完整 profile（fs/shell/web 工具与技能加载）
- 上下文压缩：由运行时按压力自动 compaction
- 流式：订阅 session.event 的 assistant/chunk(text-delta)，token 级真流式；
  消息级 assistant/message 与 final_response 仅作兜底，不重复转发

性能设计：Harness 运行时（Node 子进程）启动成本高（profile 组装数十秒），
因此按 (profile, model) 缓存单例 DeepSeekHarness 跨轮复用；知识库上下文以内联
方式注入 prompt（DSH_SYSTEM_PROMPT 只在进程启动时生效，无法按轮更新）。
"""

from __future__ import annotations

import asyncio
import shutil
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable

from ..config import settings

# airouter 等网关在历史含 reasoning 块时偶发直接关闭流（STREAM_CLOSED），
# 失败且无任何输出时自动重试一次。
_MAX_ATTEMPTS = 2
_SLICE = 16

# 快速模式用 sdk-minimal（精简独立树，无 web 搜索/skill/plan-mode，显著更快）；
# 深度模式用完整 sdk profile（工具/技能/agent-loop 全量）。
QUICK_PROFILE = 'sdk-minimal'

_clients: dict[str, object] = {}
_clients_lock = threading.Lock()


def configured() -> bool:
    """Return whether the Harness bridge is explicitly enabled."""
    return bool(settings.harness_enabled and settings.harness_home.strip())


def cleanup_stale_sessions(max_age_hours: int = 24) -> int:
    """清扫 harness-home 里的过期会话目录。

    对话记忆改由后端 DB 承载后，每轮问答都新建 harness 会话
    （sessions/<工作区>/<session_id>/），不再跨轮复用，需定期清理。
    24h  cutoff 保证不会误删进行中的轮次。返回清理数量。
    """
    root = settings.resolve_path(settings.harness_home)
    if not root.exists():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for leaf in root.glob('**/sessions/*/*'):
        try:
            if leaf.stat().st_mtime >= cutoff:
                continue
            if leaf.is_dir():
                shutil.rmtree(leaf, ignore_errors=True)
            else:
                leaf.unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue
    # 顺带清掉变空的工作区父目录
    for parent in root.glob('**/sessions/*'):
        try:
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            continue
    return removed


def profile_for_thinking(thinking: str) -> str:
    return QUICK_PROFILE if thinking == 'quick' else (settings.harness_profile or 'sdk')


def _credentials() -> tuple[str, str]:
    """Harness 走 deepseek-official 适配器（OpenAI 线协议），凭据优先 DEEPSEEK_*，回落 OPENAI_*。"""
    if settings.deepseek_api_key:
        api_key, base_url = settings.deepseek_api_key, settings.deepseek_base_url
    else:
        api_key, base_url = settings.openai_api_key, settings.openai_base_url
    if not api_key:
        raise RuntimeError('Harness 已启用，但后端未配置模型 API Key（DEEPSEEK_API_KEY 或 OPENAI_API_KEY）')
    # deepseek 适配器直接拼接 /chat/completions，baseURL 必须以 /v1 结尾
    base = (base_url or '').rstrip('/')
    if base and not base.endswith('/v1'):
        base += '/v1'
    return api_key, base


def _shared_workspace():
    root = settings.resolve_path('./harness-workspaces') / 'shared'
    root.mkdir(parents=True, exist_ok=True)
    return root


def _build_client(model: str, profile: str):
    try:
        from deepseek_harness import DeepSeekHarness
    except ImportError as exc:
        raise RuntimeError('Harness SDK 未安装，请部署 deepseek-harness-sdk/runtime') from exc
    api_key, base_url = _credentials()
    home = settings.resolve_path(settings.harness_home)
    # sdk-minimal 的会话存储压缩格式与完整 profile 不同，共用 home 会互相报
    # "uses .jsonl.zstd ... compression none"，按 profile 隔离子目录
    if profile == QUICK_PROFILE:
        home = home / 'sdk-minimal'
    home.mkdir(parents=True, exist_ok=True)
    kwargs: dict = {
        'provider': settings.harness_provider,
        'model': model or settings.harness_model,
        'profile': profile,
        'dsh_home': str(home),
        'cwd': str(_shared_workspace()),
        'runtime_cwd': str(home),
        'api_key': api_key,
        'max_tokens': settings.harness_max_tokens,
        'env': {
            'DSH_SYSTEM_PROMPT': (
                '你是 cola 知识库的智能助手。你的回答使用中文，简洁准确。'
                '用户消息中可能附带「资料库上下文」或「全网检索要求」，请严格遵循其中的指示。'
            ),
            'DSH_MAX_TOKENS_AS_SUCCESS': 'true',
            # agent web_search 工具（Exa）的密钥；空串时工具不可用，模型自行降级
            **({'EXA_API_KEY': settings.exa_api_key} if settings.exa_api_key else {}),
        },
        'initialize_timeout_seconds': 120,
        'request_timeout_seconds': 240,
    }
    if base_url:
        kwargs['base_url'] = base_url
    if settings.harness_dsh_bin.strip():
        kwargs['dsh_bin'] = settings.harness_dsh_bin.strip()
    else:
        try:
            from deepseek_harness_runtime import resolve_bundled_launch_args
            kwargs['_launch_args'] = (*resolve_bundled_launch_args(settings.harness_runtime_mode), '--profile', profile)
        except (FileNotFoundError, ValueError) as exc:
            raise RuntimeError(f'Harness runtime 不可用：{exc}') from exc
    if settings.harness_reasoning_effort:
        kwargs['reasoning_effort'] = settings.harness_reasoning_effort
    client = DeepSeekHarness(**kwargs)
    client.start()
    return client


def _client_key(model: str, profile: str) -> str:
    return f'{profile}:{model or settings.harness_model}'


def _get_client(model: str, profile: str):
    """按 (profile, model) 缓存的运行时单例；子进程崩溃后丢弃重建。"""
    key = _client_key(model, profile)
    with _clients_lock:
        client = _clients.get(key)
        if client is None:
            started = time.monotonic()
            client = _build_client(model, profile)
            print(f'[harness] runtime started for {key} in {time.monotonic() - started:.1f}s', flush=True)
            _clients[key] = client
        return client


def _drop_client(model: str, profile: str) -> None:
    key = _client_key(model, profile)
    with _clients_lock:
        client = _clients.pop(key, None)
    if client is not None:
        try:
            client.close()
        except Exception:
            pass


def _forward(notification, emit: Callable[[str, str], None]) -> None:
    """把 session.event 通知分类为 ('text', 增量文本) 或 ('progress', 进度标签)。

    - assistant/chunk(text-delta)：token 级增量文本，直出
    - step/start、tool/call、tool/result、llm/retry-started、compaction/start：
      转为中文进度标签，供前端对齐 harness 前端的过程展示
    - 完整 assistant/message 不转发（增量已覆盖时会重复）；若运行时不发
      chunk，由 stream_answer 的 final_response 兜底补发
    """
    payload = notification.payload
    event = payload.get('event') if isinstance(payload, dict) else None
    if not isinstance(event, dict):
        return
    etype = event.get('type') or ''
    data = event.get('data') or {}
    if etype == 'assistant/chunk':
        chunk = data.get('chunk') or {}
        if chunk.get('type') == 'text-delta':
            text = chunk.get('text') or ''
            if text:
                emit('text', text)
        return
    if etype == 'step/start':
        emit('progress', '正在思考…')
    elif etype == 'tool/call':
        name = data.get('tool') or data.get('name') or data.get('toolName') or ''
        emit('progress', f'正在调用工具 {name}…' if name else '正在调用工具…')
    elif etype == 'tool/result':
        emit('progress', '工具执行完成，正在整理回答…')
    elif etype in ('llm/retry', 'llm/retry-started'):
        emit('progress', '网络波动，正在重试…')
    elif etype == 'compaction/start':
        emit('progress', '正在压缩上下文…')
    elif etype == 'subagent/descriptor':
        emit('progress', '正在派遣子智能体…')


def _run_turn(prompt: str, session_id: str, model: str, profile: str, emit: Callable[[str, str], None]):
    """同步执行一轮 Harness agent turn（在线程中调用）。子进程级错误时重建单例并重抛。"""
    client = _get_client(model, profile)
    try:
        return client.run(prompt, session_id=session_id, on_notification=lambda n: _forward(n, emit))
    except Exception:
        _drop_client(model, profile)
        raise


async def stream_answer(question: str, system: str, session_id: str = '', model: str = '', thinking: str = 'quick') -> AsyncIterator[dict]:
    """流式执行一轮 Harness agent turn。

    产出 {'kind':'text','text':...}（增量文本）与 {'kind':'progress','text':...}
    （过程标签）。text-delta 直出；无增量时 final_response 兜底切片补发。
    """
    if not configured():
        raise RuntimeError('Harness 未启用')
    session_id = session_id or uuid.uuid4().hex
    profile = profile_for_thinking(thinking)
    prompt = f'{system}\n\n用户问题：{question}' if system.strip() else question

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

    def emit(kind: str, text: str) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (kind, text))

    streamed: list[str] = []
    last_error: Exception | None = None
    for _attempt in range(_MAX_ATTEMPTS):
        started = time.monotonic()
        # 重试必须用新会话：attempt 1 若已创建会话再中途失败，
        # 同名 session/prompt 会被运行时拒绝（"session already exists"）
        turn_session = session_id if _attempt == 0 else f'{session_id}-a{_attempt}'
        task = asyncio.ensure_future(asyncio.to_thread(_run_turn, prompt, turn_session, model, profile, emit))
        while not task.done():
            try:
                kind, text = await asyncio.wait_for(queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            if kind == 'text':
                streamed.append(text)
            yield {'kind': kind, 'text': text}
        while not queue.empty():
            kind, text = queue.get_nowait()
            if kind == 'text':
                streamed.append(text)
            yield {'kind': kind, 'text': text}
        try:
            result = task.result()
        except Exception as exc:  # SDK/运行时级错误
            last_error = exc
            result = None
        print(f'[harness] turn session={session_id} profile={profile} attempt={_attempt + 1} '
              f'finish={getattr(result, "finish_reason", None)} streamed={len(streamed)} '
              f'elapsed={time.monotonic() - started:.1f}s', flush=True)
        if result is not None and result.finish_reason == 'completed':
            final = (result.final_response or '').strip()
            # 兜底：运行时未发 text-delta（或通知丢失）时，用最终答复补发
            if final and not any(final in s or s in final for s in streamed):
                for i in range(0, len(final), _SLICE):
                    yield {'kind': 'text', 'text': final[i:i + _SLICE]}
            return
        if streamed:
            # 已有内容产出，视为部分成功，不重试
            return
        last_error = last_error or RuntimeError(getattr(result, 'finish_reason', None) or 'Harness 回答失败')
        # 注意：error-finish 多数是上游过载/断流（临时），不是运行时损坏。
        # 不在此处丢弃单例——重建需 60s 级冷启动且会引发 session 冲突，
        # 异常级的真损坏由 _run_turn 的 except 分支丢弃
        if _attempt + 1 < _MAX_ATTEMPTS:
            yield {'kind': 'progress', 'text': '网络波动，正在重试…'}
    raise RuntimeError(f'Harness 问答链路失败：{last_error}')
