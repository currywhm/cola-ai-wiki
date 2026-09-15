"""Server-side web search adapter used by the web Q&A mode.

The mini program never talks to a search provider directly.  The backend
fetches a small, source-preserving result set and injects it into the
server-side Harness prompt.  Provider credentials therefore stay on the
server.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus

import httpx

from ..config import settings


class WebSearchError(RuntimeError):
    """A user-safe error raised when the configured web search is unavailable."""


def _clean(value: Any, limit: int = 600) -> str:
    return " ".join(str(value or "").split())[:limit]


def _normalize(items: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in items:
        title = _clean(item.get("title"), 160)
        url = _clean(item.get("url"), 1000)
        snippet = _clean(item.get("snippet") or item.get("content") or item.get("description"), 800)
        if not title or not url or not snippet:
            continue
        results.append({"id": f"web-{len(results) + 1}", "title": title, "url": url, "snippet": snippet})
        if len(results) >= limit:
            break
    return results


async def _tavily(query: str, limit: int) -> list[dict[str, Any]]:
    if not settings.web_search_api_key:
        raise WebSearchError("问全网尚未配置搜索服务，请在后端设置 WEB_SEARCH_API_KEY")
    endpoint = f"{settings.web_search_base_url.rstrip('/')}/search"
    payload = {"query": query, "search_depth": "basic", "max_results": limit, "include_answer": False}
    async with httpx.AsyncClient(timeout=settings.web_search_timeout_seconds, follow_redirects=True) as client:
        response = await client.post(endpoint, headers={"Authorization": f"Bearer {settings.web_search_api_key}"}, json=payload)
        response.raise_for_status()
        return _normalize(response.json().get("results", []), limit)


async def _brave(query: str, limit: int) -> list[dict[str, Any]]:
    if not settings.web_search_api_key:
        raise WebSearchError("问全网尚未配置搜索服务，请在后端设置 WEB_SEARCH_API_KEY")
    endpoint = f"{settings.web_search_base_url.rstrip('/')}/res/v1/web/search"
    async with httpx.AsyncClient(timeout=settings.web_search_timeout_seconds, follow_redirects=True) as client:
        response = await client.get(
            endpoint,
            params={"q": query, "count": limit},
            headers={"Accept": "application/json", "X-Subscription-Token": settings.web_search_api_key},
        )
        response.raise_for_status()
        return _normalize(response.json().get("web", {}).get("results", []), limit)


async def _duckduckgo(query: str, limit: int) -> list[dict[str, Any]]:
    endpoint = settings.web_search_base_url.rstrip("/") or "https://api.duckduckgo.com"
    async with httpx.AsyncClient(timeout=settings.web_search_timeout_seconds, follow_redirects=True) as client:
        response = await client.get(
            endpoint,
            params={"q": query, "format": "json", "no_html": 1, "no_redirect": 1, "skip_disambig": 1},
            headers={"User-Agent": "ColaKnowledge/1.0"},
        )
        response.raise_for_status()
        payload = response.json()
    items: list[dict[str, Any]] = []
    abstract = payload.get("AbstractText")
    abstract_url = payload.get("AbstractURL")
    if abstract and abstract_url:
        items.append({"title": payload.get("Heading") or query, "url": abstract_url, "snippet": abstract})
    for topic in payload.get("RelatedTopics", []):
        if "Topics" in topic:
            for child in topic.get("Topics", []):
                items.append(child)
        else:
            items.append(topic)
    return _normalize(items, limit)


async def search_web(query: str, limit: int = 5) -> list[dict[str, Any]]:
    query = " ".join(query.split()).strip()
    if not query:
        raise WebSearchError("请输入要搜索的问题")
    try:
        provider = settings.web_search_provider
        if provider == "tavily":
            results = await _tavily(query, limit)
        elif provider == "brave":
            results = await _brave(query, limit)
        else:
            results = await _duckduckgo(query, limit)
    except WebSearchError:
        raise
    except httpx.TimeoutException as exc:
        raise WebSearchError("全网搜索连接超时，请稍后重试") from exc
    except httpx.HTTPStatusError as exc:
        raise WebSearchError(f"全网搜索服务返回异常（{exc.response.status_code}）") from exc
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise WebSearchError("全网搜索暂时不可用，请稍后重试") from exc
    if not results:
        raise WebSearchError("没有找到可引用的公开网页结果，请换个问法重试")
    return results


def web_context(results: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        f"[{index}] {item['title']}\n网址：{item['url']}\n摘要：{item['snippet']}"
        for index, item in enumerate(results, 1)
    )
