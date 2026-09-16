#!/usr/bin/env python3
"""检查小程序源码里会触发"缺失 babel helper"的写法。

背景：开发者工具把 ES6 语法转成 ES5 时，会通过 `loadBabelMod()` 注入
`@babel/runtime` 的 helper 模块。本地开发者工具（2.02.x + 基础库 3.17.x）
无法注入 `slicedToArray`，只要有一个页面用到数组解构，该页面就会在模块加载
阶段直接抛错：`module '@babel/runtime/helpers/arrayWithHoles.js' is not defined`，
`Page({...})` 不会执行，页面白屏（只剩底部 tabBar）。

已经踩过的三个点：pages/chat、pages/mine、pages/tips。写法改成
`.then((pair) => { const a = pair[0]; const b = pair[1] })` 即可。

用法：
    python3 scripts/check_mp_helpers.py            # 只扫描，报告违规
    python3 scripts/check_mp_helpers.py --quiet    # 无违规时不输出
退出码非 0 表示存在违规写法。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'miniprogram'
SKIP_DIRS = {'node_modules', 'miniprogram_npm', '_archive'}

# 会触发 slicedToArray 的数组解构：箭头函数参数、变量声明
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ('箭头函数参数用了数组解构', re.compile(r'\(\[\s*[A-Za-z_$][^()\[\]]*\]\)\s*=>')),
    ('变量声明用了数组解构', re.compile(r'\b(?:const|let|var)\s+\[[A-Za-z_$][^\[\]]*\]\s*=')),
    ('赋值用了数组解构', re.compile(r'^\s*\[[A-Za-z_$][^\[\]]*\]\s*=[^=]', re.MULTILINE)),
    # 对象剩余解构会触发 objectWithoutProperties 分支，同样不是本地稳定可用的 helper
    ('变量声明用了对象剩余解构', re.compile(r'\b(?:const|let|var)\s*\{[^{}]*\.\.\.[^{}]*\}\s*=')),
]


def iter_sources() -> list[Path]:
    files: list[Path] = []
    for path in SRC.rglob('*'):
        if not path.is_file() or path.suffix not in {'.ts', '.js'}:
            continue
        if SKIP_DIRS & set(path.relative_to(SRC).parts):
            continue
        files.append(path)
    return sorted(files)


def scan(path: Path) -> list[tuple[int, str, str]]:
    hits: list[tuple[int, str, str]] = []
    lines = path.read_text(encoding='utf-8').splitlines()
    text = '\n'.join(lines)
    for label, pattern in PATTERNS:
        for match in pattern.finditer(text):
            line_no = text.count('\n', 0, match.start()) + 1
            snippet = lines[line_no - 1].strip()
            if len(snippet) > 120:
                snippet = snippet[:117] + '...'
            hits.append((line_no, label, snippet))
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description='小程序 babel helper 兼容性检查')
    parser.add_argument('--quiet', action='store_true', help='没有问题时保持静默')
    args = parser.parse_args()

    bad = 0
    for path in iter_sources():
        for line_no, label, snippet in scan(path):
            bad += 1
            rel = path.relative_to(ROOT)
            print(f'{rel}:{line_no}: {label}')
            print(f'    {snippet}')

    if bad:
        print(f'\n发现 {bad} 处可能触发缺失 helper 的写法，改成下标取值即可（见脚本头部说明）。')
        return 1

    if not args.quiet:
        print('小程序源码 helper 检查通过：未发现数组解构等风险写法。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
