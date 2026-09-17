"""积分计费：把 deepseek-flash 的真实 token 成本换算成用户可见的「积分」。

口径（用户侧额度单位从「问答次数」改成「积分」）：
  1. 按官方「模型 & 价格」的 deepseek-flash 单价算这一轮的真实成本（元）；
  2. 用户价 = 真实成本 × credit_markup（默认 1.5 倍）；
  3. 积分 = 用户价 / credit_unit_yuan（默认 1 积分 = 0.001 元），四舍五入到整数，一轮至少 1 分。

单价（元 / 百万 tokens，官方 deepseek-flash）：
  输入（缓存命中）   空闲 0.02  高峰 0.04
  输入（缓存未命中） 空闲 1.00  高峰 2.00
  输出               空闲 4.00  高峰 8.00
高峰时段为北京时间周一至周五 9:00-12:00、14:00-18:00，其余时段（含周末）官方按半价，
所以这里按请求发生的时刻自动取价：加价倍率恒定 1.5 倍，不会因为时段不同而变。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from ..config import settings

# 北京时间固定 +8，不随服务器时区变化
BEIJING = timezone(timedelta(hours=8))
# 官方高峰时段（北京时间，周一至周五）
PEAK_WINDOWS: tuple[tuple[int, int], ...] = ((9, 12), (14, 18))

EMPTY_TOTALS: dict[str, int] = {
    'input_tokens': 0,
    'cache_read_tokens': 0,
    'cache_write_tokens': 0,
    'output_tokens': 0,
}

# harness 上报的用量字段名（camelCase）到内部字段名的映射：
# inputTokens = 未缓存输入，cacheReadTokens = 命中缓存的输入，outputTokens = 输出。
_FIELD_MAP = {
    'inputTokens': 'input_tokens',
    'cacheReadTokens': 'cache_read_tokens',
    'cacheWriteTokens': 'cache_write_tokens',
    'outputTokens': 'output_tokens',
    'input_tokens': 'input_tokens',
    'cache_read_tokens': 'cache_read_tokens',
    'cache_write_tokens': 'cache_write_tokens',
    'output_tokens': 'output_tokens',
}


def beijing_now() -> datetime:
    return datetime.now(BEIJING)


def is_peak(moment: datetime | None = None) -> bool:
    """是否落在官方高峰时段：北京时间周一至周五 9:00-12:00、14:00-18:00。"""
    local = (moment or beijing_now()).astimezone(BEIJING)
    if local.weekday() >= 5:
        return False
    hour = local.hour + local.minute / 60
    return any(start <= hour < end for start, end in PEAK_WINDOWS)


def unit_prices(moment: datetime | None = None) -> dict[str, float]:
    """本轮生效的官方单价（元 / 百万 tokens）；空闲时段是高峰价的一半。"""
    factor = 1.0 if is_peak(moment) else 0.5
    return {
        'input_hit': settings.credit_price_input_hit * factor,
        'input_miss': settings.credit_price_input_miss * factor,
        'output': settings.credit_price_output * factor,
    }


def normalize_usage(usage: Mapping[str, Any] | None) -> dict[str, int]:
    """把一次调用（或一轮汇总）的用量整理成内部字段，缺失字段按 0。"""
    totals = dict(EMPTY_TOTALS)
    for key, value in (usage or {}).items():
        field = _FIELD_MAP.get(str(key))
        if not field:
            continue
        try:
            totals[field] += max(0, int(value or 0))
        except (TypeError, ValueError):
            continue
    return totals


def sum_usage(usages: Iterable[Mapping[str, Any] | None]) -> dict[str, int]:
    """把同一轮里多次模型调用的用量（多步骤 / 重试）折成一份总量。"""
    totals = dict(EMPTY_TOTALS)
    for usage in usages:
        for key, value in normalize_usage(usage).items():
            totals[key] += value
    return totals


def usage_from_events(events: Iterable[Mapping[str, Any]] | None) -> dict[str, int]:
    """从 harness 的会话事件里取用量。

    每个 ``assistant/message`` 事件带一次模型调用的 usage（各项互不重叠），
    一轮里模型可能调用多次（工具循环），所以要全部累加。
    """
    totals = dict(EMPTY_TOTALS)
    for event in events or []:
        if not isinstance(event, Mapping) or event.get('type') != 'assistant/message':
            continue
        data = event.get('data')
        usage = data.get('usage') if isinstance(data, Mapping) else None
        if not isinstance(usage, Mapping):
            continue
        for key, value in normalize_usage(usage).items():
            totals[key] += value
    return totals


def total_tokens(totals: Mapping[str, Any] | None) -> int:
    return sum(int((totals or {}).get(key) or 0) for key in EMPTY_TOTALS)


def estimate_usage(prompt_chars: int, answer_chars: int) -> dict[str, int]:
    """拿不到运行时用量时的保守估算（兜底通道才会走到这里）。

    DeepSeek 分词器下中文约 1.5-2 字 / token、英文约 4 字 / token，这里统一按
    1 token ≈ 2 字 折算，宁可少算也不虚高；写进流水的仍是估算值，不假装是真实计量。
    """
    return {
        'input_tokens': max(0, int(prompt_chars / 2)),
        'cache_read_tokens': 0,
        'cache_write_tokens': 0,
        'output_tokens': max(0, int(answer_chars / 2)),
    }


def cost_yuan(totals: Mapping[str, Any] | None, moment: datetime | None = None) -> float:
    """这一轮的真实模型成本（元）。

    未缓存输入 + 缓存写入按「未命中」计价：官方对缓存写入不额外收费，
    写入后第一次命中前本来就按未命中价结算。
    """
    prices = unit_prices(moment)
    data = totals or {}
    miss = int(data.get('input_tokens') or 0) + int(data.get('cache_write_tokens') or 0)
    hit = int(data.get('cache_read_tokens') or 0)
    out = int(data.get('output_tokens') or 0)
    return (miss * prices['input_miss'] + hit * prices['input_hit'] + out * prices['output']) / 1_000_000


def credits_for(totals: Mapping[str, Any] | None, moment: datetime | None = None) -> int:
    """本轮扣多少积分：用户价 = 成本 × 1.5，再换算成积分（四舍五入，至少 1 分）。"""
    if total_tokens(totals) <= 0:
        return 0
    unit = max(float(settings.credit_unit_yuan or 0), 1e-9)
    user_price = cost_yuan(totals, moment) * max(float(settings.credit_markup or 0), 0)
    return max(1, int(round(user_price / unit + 1e-9)))


def quote_turn(totals: Mapping[str, Any] | None, moment: datetime | None = None) -> dict[str, Any]:
    """一次问答应记的账：用量、成本、积分、时段，写库与回给前端都用这一份。"""
    stamp = moment or beijing_now()
    data = normalize_usage(totals)
    return {
        'tokens': data,
        'total_tokens': total_tokens(data),
        'cost_yuan': round(cost_yuan(data, stamp), 6),
        'credits': credits_for(data, stamp),
        'peak': is_peak(stamp),
        'priced_at': stamp.isoformat(),
    }
