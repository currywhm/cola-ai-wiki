"""直接调用 llm.stream_answer，逐事件打时间戳，定位流式卡点。

用法: python3 scripts/test_stream.py [web|knowledge] [quick|deep]
"""
import asyncio
import sys
import time

from app.services.llm import stream_answer

mode = sys.argv[1] if len(sys.argv) > 1 else 'web'
thinking = sys.argv[2] if len(sys.argv) > 2 else 'quick'

# 与 main.py 中 web_agent 逻辑一致: web 模式强制 deep
forced = 'deep' if mode == 'web' else thinking
system = '你是 cola 的全网检索助手。' if mode == 'web' else '你是知识库助手。'
messages = [
    {'role': 'system', 'content': system},
    {'role': 'user', 'content': '你好，用一句话介绍你自己'},
]


async def main():
    t0 = time.monotonic()
    print(f'[test] mode={mode} thinking={thinking} forced={forced}', flush=True)
    try:
        async for event in stream_answer(messages, 'gpt-5.6-terra', 'test-session-cli', thinking=forced):
            dt = time.monotonic() - t0
            kind = event.get('kind')
            text = (event.get('text') or '')[:60].replace('\n', '\\n')
            print(f'[{dt:6.1f}s] {kind}: {text}', flush=True)
    except Exception as exc:
        dt = time.monotonic() - t0
        print(f'[{dt:6.1f}s] EXCEPTION {type(exc).__name__}: {exc}', flush=True)
    dt = time.monotonic() - t0
    print(f'[test] done in {dt:.1f}s', flush=True)


asyncio.run(main())
