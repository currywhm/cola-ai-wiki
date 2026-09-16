"""DeepSeek Harness bridge: full agent turns with session persistence, tools and skills.

对齐 deepseek-harness 完整版核心链路（输入 → 思考 → 任务分发 → 技能加载/注入 →
工具执行 → 回答），把运行时事件如实转成前端可渲染的「过程流」：

- 智能体：统一走完整 ``sdk`` profile（fs/shell/web/skill/plan/subagent 工具全量），
  运行时按 (profile, model) 单例缓存，跨轮复用，冷启动成本只付一次。
- 思考过程：``assistant/message.stream`` 里的 ``reasoning-chunks`` / ``text-chunks``
  是运行时压缩保存的「带时序的原始流」，按 ``dt`` 节奏回放，前端即可得到与
  harness 前端一致的逐字输出，而不是等整段回答结束才出现。
- 执行过程：``step/start`` ``tool/call`` ``tool/result`` ``compaction/*``
  ``subagent.started`` ``subagent.finished`` 逐条转成中文过程节点。
- 技能：技能包放在 ``app/harness_skills/``，启动/每轮同步到 ``$DSH_HOME/skills``
  （filesystem provider 的 user-dsh 根），运行时的 skill 目录注入会产生
  ``user/message``（source.kind=skill-catalog），模型再通过 ``skill`` 工具加载；
  前端选中的技能由后端写成「先加载该技能」的硬性指令，保证真的加载与注入。

性能与稳定性：运行时（Node 子进程）启动成本高，因此按 (profile, model)
缓存单例；知识库上下文以内联方式注入 prompt（``DSH_SYSTEM_PROMPT`` 只在进程
启动时生效，无法按轮更新）。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from ..config import settings

# airouter 等网关在历史含 reasoning 块时偶发直接关闭流（STREAM_CLOSED），
# 失败且无任何输出时自动重试一次。
_MAX_ATTEMPTS = 2

# 回放节奏：运行时把整段模型流（含时序）存在 assistant/message 里，这里按
# 分组回放。目标：既有逐字观感，又不会把整轮耗时再叠加一遍。
_TEXT_GROUP = 6
_TEXT_PACE = 0.035
_REASON_GROUP = 8
_REASON_PACE = 0.02
_PACE_BUDGET = 2.4

# 前端技能面板 <-> 磁盘技能包（app/harness_skills/<slug>/SKILL.md）
SKILL_LABELS = {
    'organize-knowledge': '整理知识库',
    'write-report': '撰写报告',
    'make-deck': '生成 PPT',
    'knowledge-diagram': '知识图解',
}

# 工具名 -> 中文动作。未列出的工具统一显示「调用工具」。
_TOOL_TITLES = {
    'skill': '加载技能',
    'web_search': '检索全网资料',
    'web_fetch': '读取网页内容',
    'bash': '执行命令',
    'read': '读取文件',
    'write': '写入文件',
    'edit': '修改文件',
    'grep': '检索文件内容',
    'glob': '查找文件',
    'read_image': '识别图片',
    'subagent': '派遣子智能体',
    'subagent_fork': '派遣子智能体',
    'send_message': '与子智能体通信',
    'todo_write': '更新任务清单',
    'create_goal': '设定任务目标',
    'update_goal': '更新任务目标',
    'get_goal': '读取任务目标',
    'workflow': '执行工作流',
    'plan': '制定执行计划',
    'exit_plan_mode': '完成计划',
}

_SKILLS_SYNCED: set[str] = set()
_clients: dict[str, object] = {}
_clients_lock = threading.Lock()


def configured() -> bool:
    """Return whether the Harness bridge is explicitly enabled."""
    return bool(settings.harness_enabled and settings.harness_home.strip())


def skills_source_dir() -> Path:
    return Path(__file__).resolve().parent.parent / 'harness_skills'


def skills_target_dir() -> Path:
    """``$DSH_HOME/skills``：dsh-skill-filesystem 的 user-dsh 扫描根。"""
    return settings.resolve_path(settings.harness_home) / 'skills'


def sync_skills(force: bool = False) -> int:
    """把仓库内的技能包同步到运行时技能根目录（幂等，内容变化即覆盖）。

    技能属于「运行时资产」：智能体对工作目录有写权限，因此每轮都从仓库
    源目录重新同步一次，避免被误改后影响后续问答。返回同步的技能数量。
    """
    source = skills_source_dir()
    if not source.is_dir():
        return 0
    target = skills_target_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 0
    synced = 0
    for entry in sorted(source.iterdir()):
        if entry.name.startswith('.'):
            continue
        if entry.is_dir():
            if not (entry / 'SKILL.md').is_file():
                continue
            dest = target / entry.name
            try:
                # 先清后拷：技能包内容变更（含删除的资源）必须完整生效
                if dest.exists():
                    shutil.rmtree(dest, ignore_errors=True)
                shutil.copytree(entry, dest)
                synced += 1
            except OSError:
                continue
        elif entry.suffix == '.md' and entry.name != 'README.md':
            try:
                shutil.copyfile(entry, target / entry.name)
                synced += 1
            except OSError:
                continue
    return synced


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
    """统一使用完整 profile：快速与深度都需要技能/工具/思考过程可见。"""
    return settings.harness_profile or 'sdk'


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


def _shared_workspace() -> Path:
    root = settings.resolve_path('./harness-workspaces') / 'shared'
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ensure_sdk_paths() -> None:
    """允许直接用仓库内的 SDK 源码运行：把配置的源码目录加入 sys.path。

    生产部署推荐 ``pip install`` 对应的 SDK 包，那时这两个配置留空即可。
    """
    import sys

    for raw in (settings.harness_sdk_path, settings.harness_runtime_sdk_path):
        value = (raw or '').strip()
        if not value:
            continue
        path = settings.resolve_path(value)
        if path.is_dir() and str(path) not in sys.path:
            sys.path.append(str(path))


def _build_client(model: str, profile: str):
    _ensure_sdk_paths()
    try:
        from deepseek_harness import DeepSeekHarness
    except ImportError as exc:
        raise RuntimeError('Harness SDK 未安装，请部署 deepseek-harness-sdk/runtime') from exc
    api_key, base_url = _credentials()
    home = settings.resolve_path(settings.harness_home)
    home.mkdir(parents=True, exist_ok=True)
    sync_skills()
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
                '【语言要求】你的思考过程（reasoning）与最终回答都必须使用简体中文，思考过程中不要写英文。'
                '你是 cola 知识库的智能助手，回答结论先行、排版清晰。'
                '用户消息中会给出本轮的「任务上下文与要求」，请严格遵循其中的指示：'
                '是否需要联网检索、是否只能依据给定资料作答、是否必须先加载某个技能。'
                '只有在任务确实需要时才调用工具，不要为了了解环境而反复执行命令。'
            ),
            'DSH_MAX_TOKENS_AS_SUCCESS': 'true',
        },
        'initialize_timeout_seconds': 180,
        'request_timeout_seconds': 600,
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


def close_clients() -> None:
    with _clients_lock:
        clients = list(_clients.items())
        _clients.clear()
    for _key, client in clients:
        try:
            client.close()
        except Exception:
            pass


class _TraceState:
    """一轮问答里过程节点的去重与配对状态。"""

    __slots__ = ('step', 'tools', 'reason_streamed', 'text_streamed', 'skills', 'pace_spent')

    def __init__(self) -> None:
        self.step = 0
        self.tools: dict[str, tuple[str, str]] = {}
        self.reason_streamed = False
        self.text_streamed = False
        self.skills: set[str] = set()
        self.pace_spent = 0.0


def _parse_arguments(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _shorten(value, limit: int = 48) -> str:
    text = str(value or '').replace('\n', ' ').strip()
    return text if len(text) <= limit else text[: limit - 1] + '…'


def _tool_detail(name: str, args: dict) -> str:
    """只暴露对用户有意义的参数，不把命令原文/文件内容丢给前端。"""
    if name == 'web_search':
        return _shorten(args.get('query') or args.get('q'), 40)
    if name == 'web_fetch':
        return _shorten(args.get('url'), 48)
    if name in ('read', 'write', 'edit', 'read_image'):
        path = str(args.get('file_path') or args.get('path') or '')
        return _shorten(path.split('/')[-1], 32) if path else ''
    if name in ('subagent', 'subagent_fork'):
        return _shorten(args.get('description') or args.get('prompt'), 40)
    if name == 'todo_write':
        todos = args.get('todos')
        return f'{len(todos)} 项' if isinstance(todos, list) else ''
    return ''


def _skill_label(slug: str) -> str:
    return SKILL_LABELS.get(slug, slug)


def _emit_pieces(state, emit, texts, kind: str, group: int, pace: float) -> None:
    """把一段打包的增量文本按小组回放，兼顾逐字观感与总耗时。"""
    if not texts:
        return
    pieces = [str(item) for item in texts if item]
    if not pieces:
        return
    groups = [pieces[i:i + group] for i in range(0, len(pieces), group)]
    # 整轮回放预算有限：预算用尽后直接吐剩余内容，避免叠加过多感知耗时
    remaining = max(0.0, _PACE_BUDGET - state.pace_spent)
    step_pace = min(pace, remaining) if remaining > 0 else 0.0
    state.pace_spent += step_pace * (len(groups) - 1)
    for index, chunk in enumerate(groups):
        piece = ''.join(chunk)
        pace_here = step_pace if index + 1 < len(groups) else 0.0
        if kind == 'reason':
            emit('trace', {'item': {'kind': 'reason', 'text': piece}}, pace_here)
        else:
            emit('text', {'text': piece}, pace_here)
    if kind == 'reason':
        state.reason_streamed = True
    else:
        state.text_streamed = True


def _walk_message_stream(data: dict, state: _TraceState, emit: Callable) -> None:
    """解析 assistant/message.stream：运行时保存的带时序原始模型流。"""
    stream = data.get('stream')
    if isinstance(stream, list):
        for record in stream:
            if not isinstance(record, dict):
                continue
            rtype = record.get('type')
            if rtype == 'reasoning-chunks':
                _emit_pieces(state, emit, record.get('texts'), 'reason', _REASON_GROUP, _REASON_PACE)
            elif rtype == 'text-chunks':
                _emit_pieces(state, emit, record.get('texts'), 'text', _TEXT_GROUP, _TEXT_PACE)
            # tool-call-chunks 由紧随其后的 tool/call 事件统一上报，避免重复节点
    # 运行时未保存流（老版本/异常路径）时，退回整段消息内容
    message = data.get('message') or {}
    blocks = message.get('content') or []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get('type')
        text = block.get('text') or ''
        if btype == 'reasoning' and text and not state.reason_streamed:
            emit('trace', {'item': {'kind': 'reason', 'text': text}}, 0.0)
            state.reason_streamed = True
        elif btype == 'text' and text and not state.text_streamed:
            emit('text', {'text': text}, 0.0)
            state.text_streamed = True


def _forward(notification, emit: Callable, state: _TraceState) -> None:
    """把运行时通知分类为前端可渲染的过程节点与回答增量文本。

    - ``assistant/message``：思考过程 + 回答增量（按记录分组回放）
    - ``step/start``、``tool/call``、``tool/result``、``compaction/*``：
      执行过程节点
    - ``user/message``（source.kind=skill-catalog）：技能目录注入
    - ``subagent.started`` / ``subagent.finished``：任务分发
    """
    method = getattr(notification, 'method', '') or ''
    payload = getattr(notification, 'payload', None) or {}
    if method == 'subagent.started':
        emit('trace', {'item': {'kind': 'agent', 'state': 'run', 'title': '派遣子智能体处理子任务'}}, 0.0)
        return
    if method == 'subagent.finished':
        emit('trace', {'item': {'kind': 'agent', 'state': 'done', 'title': '子智能体已返回结果'}}, 0.0)
        return
    if method != 'session.event':
        return
    event = payload.get('event') if isinstance(payload, dict) else None
    if not isinstance(event, dict):
        return
    etype = event.get('type') or ''
    data = event.get('data') or {}
    if not isinstance(data, dict):
        return

    if etype == 'user/message':
        source = data.get('source') or {}
        skind = source.get('kind') if isinstance(source, dict) else ''
        if skind == 'skill-catalog':
            entries = source.get('entries') or []
            names = [ _skill_label(str(e.get('name') or '')) for e in entries if isinstance(e, dict) ]
            emit('trace', {'item': {
                'kind': 'skill', 'state': 'catalog',
                # 目录只把「运行时有哪些技能包」告知模型，并不等于把技能注入本轮；
                # 真正生效的只有用户选中的那一个，措辞上不要混。
                'title': f'技能目录：{len(names)} 项可用' if names else '技能目录：暂无可用技能',
                'detail': '、'.join(names[:4]),
            }}, 0.0)
        elif skind == 'skill-invocation':
            name = str(source.get('name') or source.get('skill') or '')
            emit('trace', {'item': {'kind': 'skill', 'state': 'load', 'title': f'加载技能 · {_skill_label(name)}'}}, 0.0)
        return

    if etype == 'step/start':
        state.step = int(data.get('step') or state.step + 1)
        emit('trace', {'item': {'kind': 'step', 'index': state.step, 'title': f'第 {state.step} 步'}}, 0.0)
        return

    if etype == 'assistant/message':
        _walk_message_stream(data, state, emit)
        return

    if etype == 'assistant/attempt':
        state.reason_streamed = False
        _walk_message_stream(data, state, emit)
        return

    if etype == 'tool/call':
        call_id = str(data.get('callId') or '')
        name = str(data.get('name') or '')
        args = _parse_arguments(data.get('arguments'))
        title = _TOOL_TITLES.get(name, '调用工具')
        detail = _tool_detail(name, args)
        state.tools[call_id] = (name, title)
        if name == 'skill':
            slug = str(args.get('name') or '')
            state.skills.add(slug)
            emit('trace', {'item': {
                'kind': 'skill', 'state': 'load',
                'title': f'加载技能 · {_skill_label(slug)}' if slug else '加载技能',
                'detail': '',
            }}, 0.0)
        else:
            emit('trace', {'item': {'kind': 'tool', 'state': 'run', 'name': name, 'title': title, 'detail': detail}}, 0.0)
        return

    if etype == 'tool/result':
        message = data.get('message') or {}
        source = message.get('source') or {}
        call_id = str(source.get('callId') or '')
        blocks = message.get('content') or []
        if not call_id and blocks and isinstance(blocks[0], dict):
            call_id = str(blocks[0].get('toolCallId') or '')
        name, title = state.tools.get(call_id, ('', ''))
        failed = bool(data.get('error')) or any(
            isinstance(block, dict) and block.get('isError') for block in blocks
        )
        if name == 'skill':
            emit('trace', {'item': {
                'kind': 'skill', 'state': 'error' if failed else 'done',
                'title': '技能加载失败' if failed else (f'{title}完成' if title else '技能已加载'),
            }}, 0.0)
        elif name:
            emit('trace', {'item': {
                'kind': 'tool', 'state': 'error' if failed else 'done',
                'name': name, 'title': f'{title}失败' if failed else f'{title}完成',
            }}, 0.0)
        return

    if etype == 'compaction/start':
        emit('trace', {'item': {'kind': 'note', 'state': 'run', 'title': '正在压缩上下文'}}, 0.0)
        return
    if etype == 'compaction/end':
        emit('trace', {'item': {'kind': 'note', 'state': 'done', 'title': '上下文已压缩'}}, 0.0)
        return
    if etype in ('llm/retry', 'llm/retry-started'):
        emit('trace', {'item': {'kind': 'note', 'state': 'run', 'title': '网络波动，正在重试'}}, 0.0)
        return
    if etype == 'turn/end':
        reason = (data.get('reason') or {}) if isinstance(data.get('reason'), dict) else {}
        error = reason.get('error') or {}
        if reason.get('kind') == 'error' and isinstance(error, dict) and error.get('message'):
            emit('trace', {'item': {'kind': 'note', 'state': 'error', 'title': _shorten(error.get('message'), 120)}}, 0.0)


def _run_turn(prompt: str, session_id: str, model: str, profile: str, emit: Callable):
    """同步执行一轮 Harness agent turn（在线程中调用）。子进程级错误时重建单例并重抛。"""
    client = _get_client(model, profile)
    state = _TraceState()
    try:
        return client.run(prompt, session_id=session_id, on_notification=lambda n: _forward(n, emit, state))
    except Exception:
        _drop_client(model, profile)
        raise


def _skill_instruction(skill: str) -> str:
    slug = (skill or '').strip()
    if not slug:
        return ''
    label = _skill_label(slug)
    return (
        f'本轮必须使用技能「{label}」（skill 名称：{slug}）：'
        f'第一步就调用 skill 工具加载它，再严格按照该技能的步骤与输出格式完成任务，'
        f'不要跳过技能直接自由发挥。'
    )


def _custom_skill_instruction(skill_prompt: str, skill_label: str) -> str:
    """用户自己创建/收藏的技能：直接把技能指令作为本轮的硬约束注入。

    自定义技能不是运行时技能包，指令本身就定义了这个技能，所以要把话说死：
    让模型直接用这段指令干活，不要再去加载同名技能包，否则会多出一段无意义的加载失败过程。
    """
    label = (skill_label or '').strip() or '用户选择的技能'
    note = '这是用户自定义技能，定义就是下面这段指令，不要再调用 skill 工具或加载同名技能包。'
    return (
        f'本轮启用了技能「{label}」，必须严格按下面的技能指令执行，不要跳过，也不要改写技能目标：\n'
        f'{skill_prompt.strip()}'
        f'（{note}）'
    )


def _build_prompt(question: str, system: str, skill: str, mode: str, skill_prompt: str = '', skill_label: str = '') -> str:
    parts: list[str] = []
    if system.strip():
        parts.append(system.strip())
    if mode == 'planner':
        parts.append(
            '本轮任务模式：执行规划（通用智能体）。可以使用可用工具（联网检索、网页读取、技能、'
            '任务清单、子智能体等）先规划再执行；需要外部事实时必须检索，不得编造；'
            '不要为了了解环境而反复执行命令，工具调用应服务于任务本身。'
        )
    else:
        parts.append(
            '本轮任务模式：基于知识库资料问答。请直接依据上方给出的资料作答，'
            '不要为了了解环境而执行命令或浏览文件系统；资料不足时明确说明。'
            '本轮不要主动调用 skill 工具去加载技能包：只有用户明确选中的技能才会随指令下发。'
        )
    if skill.strip():
        parts.append(_skill_instruction(skill))
    if skill_prompt.strip():
        parts.append(_custom_skill_instruction(skill_prompt, skill_label))
    parts.append('思考与推理过程请使用简体中文（便于用户阅读过程），最终回答同样使用简体中文。')
    parts.append('用户问题：\n' + question)
    return '\n\n'.join(parts)


async def stream_answer(
    question: str,
    system: str,
    session_id: str = '',
    model: str = '',
    thinking: str = 'quick',
    skill: str = '',
    mode: str = 'knowledge',
    skill_prompt: str = '',
    skill_label: str = '',
) -> AsyncIterator[dict]:
    """流式执行一轮 Harness agent turn。

    产出 ``{'kind':'text','text':...}``（回答增量）与 ``{'kind':'trace','item':{...}}``
    （过程节点：step / reason / tool / skill / agent / note）。
    """
    if not configured():
        raise RuntimeError('Harness 未启用')
    sync_skills()
    session_id = session_id or uuid.uuid4().hex
    profile = profile_for_thinking(thinking)
    prompt = _build_prompt(question, system, skill, mode, skill_prompt, skill_label)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, dict, float]] = asyncio.Queue()

    def emit(kind: str, payload: dict, pace: float = 0.0) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (kind, payload, pace))

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
                kind, payload, pace = await asyncio.wait_for(queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            if kind == 'text':
                streamed.append(payload.get('text', ''))
            yield {'kind': kind, **payload}
            if pace:
                await asyncio.sleep(pace)
        while not queue.empty():
            kind, payload, pace = queue.get_nowait()
            if kind == 'text':
                streamed.append(payload.get('text', ''))
            yield {'kind': kind, **payload}
            if pace:
                await asyncio.sleep(pace)
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
            # 兜底：运行时未发增量文本（或通知丢失）时，用最终答复补发
            if final and not any(final in s or s in final for s in streamed):
                for i in range(0, len(final), 24):
                    yield {'kind': 'text', 'text': final[i:i + 24]}
            return
        if streamed:
            # 已有内容产出，视为部分成功，不重试
            return
        last_error = last_error or RuntimeError(getattr(result, 'finish_reason', None) or 'Harness 回答失败')
        # 注意：error-finish 多数是上游过载/断流（临时），不是运行时损坏。
        # 不在此处丢弃单例——重建需 60s 级冷启动且会引发 session 冲突，
        # 异常级的真损坏由 _run_turn 的 except 分支丢弃
        if _attempt + 1 < _MAX_ATTEMPTS:
            yield {'kind': 'trace', 'item': {'kind': 'note', 'state': 'run', 'title': '网络波动，正在重试'}}
    raise RuntimeError(f'Harness 问答链路失败：{last_error}')
