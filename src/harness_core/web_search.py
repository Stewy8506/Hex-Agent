"""Autonomous Web Search Tool for HEX AI Workbench.

Provides multi-backend live web search capabilities for autonomous coding agents:
1. DuckDuckGo: Zero-key default fallback (free, instant, works offline/online with zero credentials).
2. Tavily Search: Agent-optimized search API with markdown extraction (via `TAVILY_API_KEY`).
3. Brave Search: Independent privacy-first web index (via `BRAVE_API_KEY`).

Integrated into `ToolGateway` as an R1 (read-only) tool.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from typing import Any


def _search_duckduckgo(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """Execute a zero-key DuckDuckGo search via HTML endpoint and parse results."""
    encoded_query = urllib.parse.urlencode({"q": query})
    url = f"https://html.duckduckgo.com/html/?{encoded_query}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        # Fallback to duckduckgo instant answer json API if html endpoint fails
        return _search_duckduckgo_instant(query, max_results)

    results: list[dict[str, str]] = []
    # Pattern to find result links: <a class="result__snippet" ...> or <a class="result__url" ...>
    # Match result blocks: class="result results_links ...">
    result_blocks = re.findall(r'<div class="result results_links[^>]*>(.*?)</div>\s*</div>', html, re.DOTALL)
    for block in result_blocks:
        title_m = re.search(r'<a[^>]+class="result__a"[^>]*>(.*?)</a>', block, re.DOTALL)
        url_m = re.search(r'<a[^>]+class="result__url"[^>]+href="([^"]+)"', block, re.DOTALL)
        if not url_m:
            url_m = re.search(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"', block, re.DOTALL)
        snippet_m = re.search(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', block, re.DOTALL)

        if title_m and url_m:
            raw_title = re.sub(r"<[^>]+>", "", title_m.group(1)).strip()
            raw_url = url_m.group(1).strip()
            # Clean duckduckgo redirect url if necessary: /l/?kh=-1&uddg=...
            if "uddg=" in raw_url:
                parsed = urllib.parse.parse_qs(urllib.parse.urlparse(raw_url).query)
                if "uddg" in parsed:
                    raw_url = parsed["uddg"][0]
            elif raw_url.startswith("//"):
                raw_url = "https:" + raw_url

            snippet = ""
            if snippet_m:
                snippet = re.sub(r"<[^>]+>", "", snippet_m.group(1)).strip()

            results.append({"title": raw_title, "url": raw_url, "snippet": snippet})
            if len(results) >= max_results:
                break

    if not results:
        return _search_duckduckgo_instant(query, max_results)

    return results


def _search_duckduckgo_instant(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """Instant Answer API fallback for DuckDuckGo."""
    encoded_query = urllib.parse.urlencode({"q": query, "format": "json", "no_html": "1"})
    url = f"https://api.duckduckgo.com/?{encoded_query}"
    headers = {"User-Agent": "HEX-Agent/1.0"}
    req = urllib.request.Request(url, headers=headers)
    results: list[dict[str, str]] = []
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            # Abstract
            if data.get("AbstractText"):
                results.append(
                    {
                        "title": data.get("Heading", query),
                        "url": data.get("AbstractURL", ""),
                        "snippet": data.get("AbstractText", ""),
                    }
                )
            # Related topics
            for topic in data.get("RelatedTopics", []):
                if isinstance(topic, dict) and topic.get("Text"):
                    results.append(
                        {
                            "title": topic.get("FirstURL", "").split("/")[-1].replace("_", " "),
                            "url": topic.get("FirstURL", ""),
                            "snippet": topic.get("Text", ""),
                        }
                    )
                if len(results) >= max_results:
                    break
    except Exception:
        pass
    return results


def _search_tavily(query: str, max_results: int = 5, api_key: str | None = None) -> list[dict[str, str]]:
    """Execute a search via Tavily Search API."""
    key = api_key or os.environ.get("TAVILY_API_KEY") or os.environ.get("HEX_TAVILY_API_KEY")
    if not key:
        raise ValueError("Tavily API key is missing. Set TAVILY_API_KEY or HEX_TAVILY_API_KEY.")

    url = "https://api.tavily.com/search"
    payload = json.dumps({
        "api_key": key,
        "query": query,
        "max_results": max_results,
        "include_answer": True,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

    with urllib.request.urlopen(req, timeout=12.0) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    results: list[dict[str, str]] = []
    for item in data.get("results", []):
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("content", ""),
        })
    return results


def _search_brave(query: str, max_results: int = 5, api_key: str | None = None) -> list[dict[str, str]]:
    """Execute a search via Brave Search API."""
    key = api_key or os.environ.get("BRAVE_API_KEY") or os.environ.get("HEX_BRAVE_API_KEY")
    if not key:
        raise ValueError("Brave Search API key is missing. Set BRAVE_API_KEY or HEX_BRAVE_API_KEY.")

    encoded = urllib.parse.urlencode({"q": query, "count": max_results})
    url = f"https://api.search.brave.com/res/v1/web/search?{encoded}"
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": key,
    }
    req = urllib.request.Request(url, headers=headers, method="GET")

    with urllib.request.urlopen(req, timeout=10.0) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    results: list[dict[str, str]] = []
    for item in data.get("web", {}).get("results", []):
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("description", ""),
        })
    return results


def execute_web_search(
    query: str,
    *,
    max_results: int = 5,
    provider: str = "auto",
    api_key: str | None = None,
) -> dict[str, Any]:
    """Execute a web search across supported providers with auto-fallback.

    Args:
        query: Search term or question.
        max_results: Number of results to return (1-20).
        provider: 'auto', 'duckduckgo', 'tavily', or 'brave'.
        api_key: Optional explicit API key override.

    Returns:
        Structured search response dict with query, provider, and result list.
    """
    clean_query = query.strip()
    if not clean_query:
        return {"query": "", "provider": provider, "count": 0, "results": []}

    max_results = max(1, min(max_results, 20))
    resolved_provider = provider.lower()

    if resolved_provider == "auto":
        if os.environ.get("TAVILY_API_KEY") or os.environ.get("HEX_TAVILY_API_KEY"):
            resolved_provider = "tavily"
        elif os.environ.get("BRAVE_API_KEY") or os.environ.get("HEX_BRAVE_API_KEY"):
            resolved_provider = "brave"
        else:
            resolved_provider = "duckduckgo"

    results: list[dict[str, str]] = []
    error_msg: str | None = None

    try:
        if resolved_provider == "tavily":
            results = _search_tavily(clean_query, max_results=max_results, api_key=api_key)
        elif resolved_provider == "brave":
            results = _search_brave(clean_query, max_results=max_results, api_key=api_key)
        elif resolved_provider == "duckduckgo":
            results = _search_duckduckgo(clean_query, max_results=max_results)
        else:
            results = _search_duckduckgo(clean_query, max_results=max_results)
            resolved_provider = "duckduckgo"
    except Exception as exc:
        error_msg = str(exc)
        # Automatic fallback to duckduckgo if premium provider fails
        if resolved_provider != "duckduckgo":
            try:
                results = _search_duckduckgo(clean_query, max_results=max_results)
                resolved_provider = "duckduckgo_fallback"
                error_msg = None
            except Exception as e2:
                error_msg = f"{exc} | Fallback failed: {e2}"

    return {
        "query": clean_query,
        "provider": resolved_provider,
        "count": len(results),
        "results": results,
        "error": error_msg,
    }
