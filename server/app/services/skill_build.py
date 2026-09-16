"""新建技能：交给 harness 的 skill-creator 真的写一个技能包。

链路：
用户口述的要求 → 后端拼一段「本轮必须使用 skill-creator」的任务提示词 → 运行时在该租户
私有的工作区里写出技能包（``SKILL.md`` 必需，复杂技能再带 ``scripts/``）→ 后端把技能包从
工作区搬进该租户的运行时技能根 ``$DSH_HOME/users/<租户>/skills/<slug>`` → 落一条
``skills`` 记录（``harness=<slug>``）。之后用户在对话里选中它，走的是真正的技能包加载，
而不是把一段文字塞进提示词。

为什么不直接让模型写进技能根：
沙箱固定为 ``workspace-write``，工作根是租户工作区，技能根在工作区之外——越界写会被拒绝。
所以让模型在工作区里产出，再由后端搬运，权限边界不动。

垃圾文件：安装完成后工作区里的构建残留会一并删除；删除技能时安装目录与残留一起清掉。
"""

from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

from . import harness

# 构建目录（工作区内）与安装目录（租户技能根）分别落在这里
BUILD_ROOT = 'skills-build'
SKILL_FILE = 'SKILL.md'
FALLBACK_REFERENCE = 'references/执行要点.md'
FALLBACK_ASSET = 'assets/输出模板.md'
BUILTIN_SKILL_SLUG = 'skill-creator'
BUILTIN_SKILL_LABEL = '制作技能'

# slug 只允许我们自己生成的字形：删目录、拼路径前都要再过一遍，避免路径穿越
_SAFE_SLUG = re.compile(r'^[a-z0-9][a-z0-9-]{2,40}$')

# 一个技能包的体积上限：够写说明 + 几个脚本，又不至于把租户目录撑爆
_MAX_FILES = 24
_MAX_BYTES = 512 * 1024
_MAX_SKILL_BYTES = 120 * 1024


def new_slug() -> str:
    return f'usr-{uuid.uuid4().hex[:10]}'


def safe_slug(slug: str) -> bool:
    return bool(_SAFE_SLUG.match((slug or '').strip()))


def workspace_build_dir(user_id: str, slug: str) -> Path:
    """模型写技能包的地方（在工作区内，沙箱允许写）。"""
    return harness.user_workspace(user_id) / BUILD_ROOT / (slug or '')


def installed_dir(user_id: str, slug: str) -> Path:
    """技能包最终安装的地方（租户私有技能根，运行时每轮扫描这里）。"""
    return harness.skills_target_dir(user_id) / (slug or '')


def package_installed(user_id: str, slug: str) -> bool:
    if not safe_slug(slug):
        return False
    try:
        return (installed_dir(user_id, slug) / SKILL_FILE).is_file()
    except OSError:
        return False


def parse_frontmatter(text: str) -> dict[str, str]:
    """取 SKILL.md 顶部的 frontmatter 三件套；格式不对时返回空字典，不抛错。"""
    if not text.startswith('---'):
        return {}
    end = text.find('\n---', 3)
    if end < 0:
        return {}
    found: dict[str, str] = {}
    for line in text[3:end].splitlines():
        key, sep, value = line.partition(':')
        if not sep:
            continue
        name = key.strip().lower()
        if name in ('name', 'description', 'whentouse'):
            found[name] = value.strip().strip('"').strip("'")
    return found


def _strip_frontmatter(text: str) -> str:
    if not text.startswith('---'):
        return text.strip()
    end = text.find('\n---', 3)
    if end < 0:
        return text.strip()
    return text[end + 4:].strip()


def read_package_text(user_id: str, slug: str) -> str:
    """读安装好的 SKILL.md 正文（去掉 frontmatter），编辑页回填用。"""
    if not package_installed(user_id, slug):
        return ''
    try:
        text = (installed_dir(user_id, slug) / SKILL_FILE).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''
    return _drop_appendix(_strip_frontmatter(text))


def package_files(user_id: str, slug: str) -> list[str]:
    """技能包里的文件名（相对路径，按目录顺序），编辑页展示用。"""
    if not safe_slug(slug):
        return []
    root = installed_dir(user_id, slug)
    if not root.is_dir():
        return []
    names: list[str] = []
    try:
        for item in sorted(root.rglob('*')):
            if item.is_file():
                names.append(str(item.relative_to(root)))
    except OSError:
        return names
    return names


def install_package(user_id: str, slug: str, source: Path) -> dict:
    """把工作区里的技能包搬进租户技能根；SKILL.md 必须在，缺了直接报错。"""
    if not safe_slug(slug):
        raise ValueError('技能标识不合法')
    skill_md = source / SKILL_FILE
    if not skill_md.is_file():
        raise FileNotFoundError('技能包缺少 SKILL.md')

    text = skill_md.read_text(encoding='utf-8', errors='replace')
    if len(text.encode('utf-8')) > _MAX_SKILL_BYTES:
        text = text.encode('utf-8')[:_MAX_SKILL_BYTES].decode('utf-8', errors='ignore')

    target = installed_dir(user_id, slug)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    (target / SKILL_FILE).write_text(text, encoding='utf-8')

    files = 1
    total = len(text.encode('utf-8'))
    for item in sorted(source.rglob('*')):
        if not item.is_file() or item == skill_md:
            continue
        rel = item.relative_to(source)
        if any(part.startswith('.') for part in rel.parts):
            continue
        try:
            data = item.read_bytes()
        except OSError:
            continue
        if files >= _MAX_FILES or total + len(data) > _MAX_BYTES:
            break
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        files += 1
        total += len(data)

    return {'files': files, 'bytes': total, 'meta': parse_frontmatter(text), 'text': text}


def _has_file(folder: Path) -> bool:
    try:
        return any(item.is_file() for item in folder.iterdir())
    except OSError:
        return False


def _drop_appendix(body: str) -> str:
    """去掉上一次写入时缀在末尾的「附带文件」一节，避免反复编辑层层叠叠。"""
    marker = '\n## 附带文件'
    text = (body or '').strip()
    index = text.rfind(marker)
    if index >= 0 and text[index + 1:].startswith('## 附带文件'):
        return text[:index].strip()
    return text


def write_package_text(user_id: str, slug: str, name: str, summary: str, body: str) -> bool:
    """按编辑页改过的内容重写已安装技能包，保证技能包与数据库始终一致。

    只重写我们自己生成的兜底文件（``references/执行要点.md`` 与 ``assets/输出模板.md``）；
    harness 按用户要求写出来的其它文件（例如 ``references/条款风险清单.md``）原样保留，
    不能被编辑页覆盖掉。目录被写空、或包里只剩一个 SKILL.md 时顺手补齐三件套，
    不让技能退化成只有一个壳子。
    """
    if not package_installed(user_id, slug):
        return False
    root = installed_dir(user_id, slug)
    skill_name = (name or '').strip() or '我的技能'
    description = (summary or '').strip() or f'{skill_name}：按用户写好的要求执行。'
    content = _drop_appendix(body)
    instruction = content or f'按用户填写的技能指令执行：{skill_name}。'
    reference = root / FALLBACK_REFERENCE
    asset = root / FALLBACK_ASSET
    try:
        (root / 'references').mkdir(parents=True, exist_ok=True)
        (root / 'assets').mkdir(parents=True, exist_ok=True)
        if not _has_file(root / 'references') or reference.is_file():
            reference.write_text(compose_reference_markdown(skill_name, instruction), encoding='utf-8')
        if not _has_file(root / 'assets') or asset.is_file():
            asset.write_text(compose_asset_markdown(skill_name), encoding='utf-8')
        extra = [name for name in package_files(user_id, slug) if name != SKILL_FILE]
        inventory = ''.join(f'- `{name}`\n' for name in extra)
        text = (
            '---\n'
            f'name: {slug}\n'
            f'description: {description[:60]}\n'
            f'whenToUse: 用户需要{skill_name}这项工作时。\n'
            '---\n\n'
            f'{content or f"# {skill_name}"}\n\n'
            '## 附带文件\n'
            f'{inventory or "- （暂无）\n"}'
        )
        (root / SKILL_FILE).write_text(text, encoding='utf-8')
    except OSError:
        return False
    return True


def remove_build_dir(user_id: str, slug: str) -> None:
    """清掉工作区里的构建残留，避免同一份技能在工作区留副本。

    连空掉的 ``skills-build`` 父目录一起收掉，工作区不会慢慢堆一堆空目录。
    """
    if not safe_slug(slug):
        return
    try:
        path = workspace_build_dir(user_id, slug)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        parent = path.parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass


def remove_package(user_id: str, slug: str) -> bool:
    """删除技能时连文件一起删：安装目录 + 构建残留，不留垃圾。"""
    if not safe_slug(slug):
        return False
    removed = False
    try:
        target = installed_dir(user_id, slug)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
            removed = True
    except OSError:
        pass
    remove_build_dir(user_id, slug)
    return removed


def compose_skill_markdown(name: str, summary: str, prompt: str, slug: str) -> str:
    """兜底：运行时没写出技能包时，用用户填的内容合成一份结构合规的 SKILL.md。

    宁可给用户一个能用的技能包，也不要因为一次模型抖动让人白填一遍表单。
    frontmatter 的 name 只能是 kebab-case 英文（用 slug），中文名走 description。
    """
    skill_name = (name or '').strip() or '我的技能'
    description = (summary or '').strip() or f'{skill_name}：按用户写好的要求执行。'
    body = (prompt or '').strip() or '按用户填写的技能指令执行。'
    return (
        '---\n'
        f'name: {slug}\n'
        f'description: {description[:60]}\n'
        f'whenToUse: 用户需要{skill_name}这项工作时。\n'
        '---\n\n'
        f'# {skill_name}\n\n'
        '## 目标\n'
        f'{description[:60]}\n\n'
        '## 前提与边界\n'
        '- 只做用户要求里写明的这件事，资料不足时写明缺什么，不编造。\n\n'
        '## 执行步骤\n'
        f'{body}\n\n'
        '## 输出格式\n'
        '按 `assets/输出模板.md` 的骨架输出，逐节填写。\n\n'
        '## 附带文件\n'
        '- `references/执行要点.md`：执行时逐条对照的核对清单。\n'
        '- `assets/输出模板.md`：可直接套用的输出骨架。\n\n'
        '## 自检\n'
        '- 每个小节都填了吗？\n'
        '- 结论都有依据吗，没有依据的有没有标成待确认？\n'
        '- 有没有编造用户没提供的事实、数字或名称？\n'
        '- 格式是否与 `assets/输出模板.md` 一致？\n'
    )


def compose_reference_markdown(name: str, prompt: str) -> str:
    """兜底技能包的依据文件：把用户写的要求拆成可逐条核对的要点。"""
    skill_name = (name or '').strip() or '我的技能'
    body = (prompt or '').strip() or '按用户填写的技能指令执行。'
    return (
        f'# {skill_name} · 执行要点\n\n'
        '## 用户要求\n'
        f'{body}\n\n'
        '## 核对清单\n'
        '- 要求里提到的每一项，输出里都要能找到对应内容。\n'
        '- 数字、名称、日期一律取自用户给的材料；材料没有的写「未提供」。\n'
        '- 需要判断的地方要写出判断依据，不要只给结论。\n'
        '- 输出前通读一遍，删掉与本次要求无关的内容。\n'
    )


def compose_asset_markdown(name: str) -> str:
    """兜底技能包的输出模板：给一个能直接套用的骨架。"""
    skill_name = (name or '').strip() or '我的技能'
    return (
        f'# {skill_name} · 输出模板\n\n'
        '## 一、结论\n'
        '{{用三句话以内说清结果}}\n\n'
        '## 二、依据与要点\n'
        '- {{要点 1：写清事实与出处}}\n'
        '- {{要点 2}}\n\n'
        '## 三、下一步\n'
        '- {{建议动作、责任人、时间}}\n\n'
        '> 未提供的字段保留 {{占位符}}，不要编造。\n'
    )


def synthesize_package(user_id: str, slug: str, name: str, summary: str, prompt: str) -> Path:
    """兜底技能包：SKILL.md + references/ + assets/ 一次写全，不留只有一个壳子的技能。"""
    root = workspace_build_dir(user_id, slug)
    (root / 'references').mkdir(parents=True, exist_ok=True)
    (root / 'assets').mkdir(parents=True, exist_ok=True)
    (root / SKILL_FILE).write_text(compose_skill_markdown(name, summary, prompt, slug), encoding='utf-8')
    (root / 'references' / '执行要点.md').write_text(compose_reference_markdown(name, prompt), encoding='utf-8')
    (root / 'assets' / '输出模板.md').write_text(compose_asset_markdown(name), encoding='utf-8')
    return root


def build_prompt(instruction: str, name: str, summary: str, slug: str) -> str:
    """交给 harness 的任务描述：把要求说死，产出位置说死。"""
    out = f'{BUILD_ROOT}/{slug}'
    lines = [
        '本轮任务：制作技能。',
        f'必须使用技能「{BUILTIN_SKILL_LABEL}」（skill 名称：{BUILTIN_SKILL_SLUG}）：'
        '第一步就调用 skill 工具把它加载进来，然后严格按照它的结构与要求写出技能包。',
        f'技能包输出目录（相对当前工作目录，目录需要你自己创建）：{out}',
        f'技能包至少要写出三个文件，缺一个都不算完成：{out}/{SKILL_FILE}、'
        f'{out}/references/<名称>.md（判断依据与核对清单）、{out}/assets/<名称>.md（可直接套用的输出模板）。',
        f'{SKILL_FILE} 的 frontmatter 里 name 必须写成 {slug}（小写英文 + 连字符），运行时按这个名字加载，技能的中文名写进 description。',
        '本产品没有任何执行能力：不要写 scripts/ 目录，不要写需要运行的代码或命令。',
        '不要写到工作目录之外，不要输出与技能包无关的内容。',
    ]
    if (name or '').strip():
        lines.append(f'用户给这个技能起的名字：{name.strip()}')
    if (summary or '').strip():
        lines.append(f'用户给这个技能的一句话说明：{summary.strip()}')
    lines.append('用户对技能的要求：\n' + (instruction or '').strip())
    lines.append('做完后用一句话说明你创建了什么，并列出生成的文件名。')
    return '\n'.join(lines)


# 过程节点 → 制作阶段文案：让等待期间看到的是「在做什么」，而不是空白转圈
_STAGE_BY_TOOL = {
    'skill': '正在加载制作技能…',
    'write': '正在编写技能文件…',
    'edit': '正在修改技能文件…',
    'bash': '正在运行技能脚本…',
    'read': '正在检查技能文件…',
    'glob': '正在检查技能文件…',
    'grep': '正在检查技能文件…',
    'subagent': '正在并行完善技能…',
}


def stage_label(item: dict) -> str:
    """把 harness 过程节点翻译成一句进度文案；没有可展示的阶段时返回空串。"""
    kind = str(item.get('kind') or '')
    if kind == 'tool':
        return _STAGE_BY_TOOL.get(str(item.get('name') or ''), '正在执行…')
    if kind == 'step':
        return '正在思考技能结构…'
    if kind == 'agent':
        return '正在并行完善技能…'
    if kind == 'note':
        return str(item.get('title') or '')
    return ''
