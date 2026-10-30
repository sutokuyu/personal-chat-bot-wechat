"""Opt-in live test for the DeepSeek Search API.

Skipped unless ``WEB_SEARCH_LIVE=1`` is set and ``DEEPSEEK_API_KEY`` is present;
each run costs one Search API request.
"""

from __future__ import annotations

import os

import pytest

from app.config import WebSearchConfig, load_dotenv_if_present
from app.tools.web_tools import search_web

load_dotenv_if_present()

LIVE_ENABLED = (
    os.environ.get("WEB_SEARCH_LIVE") == "1"
    and bool(os.environ.get("DEEPSEEK_API_KEY"))
)

requires_live_search = pytest.mark.skipif(
    not LIVE_ENABLED,
    reason="live web search: set WEB_SEARCH_LIVE=1 and DEEPSEEK_API_KEY",
)


@requires_live_search
async def test_live_search_returns_results_for_a_news_query():
    payload = await search_web(WebSearchConfig(max_results=3), "DeepSeek V4 发布")

    results = payload.get("results") or []
    assert results, "search returned no results"
    assert results[0].get("title")
    assert str(results[0].get("url", "")).startswith("http")
    print(results[0]["title"], results[0]["url"], flush=True)
