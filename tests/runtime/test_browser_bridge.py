"""Browser bridge unit tests — no live browser or Playwright install required."""

from __future__ import annotations

import importlib
import os

import pytest

from cluxion_agentplugin_preprocessing import plugin, schemas
from cluxion_runtime.web import browser_bridge

BROWSER_TOOL_NAMES = [
    "cluxion_web_search",
    "cluxion_browser_open",
    "cluxion_browser_extract",
    "cluxion_browser_click",
    "cluxion_browser_type",
]

SCHEMA_BY_NAME = {
    "cluxion_web_search": schemas.WEB_SEARCH_SCHEMA,
    "cluxion_browser_open": schemas.BROWSER_OPEN_SCHEMA,
    "cluxion_browser_extract": schemas.BROWSER_EXTRACT_SCHEMA,
    "cluxion_browser_click": schemas.BROWSER_CLICK_SCHEMA,
    "cluxion_browser_type": schemas.BROWSER_TYPE_SCHEMA,
}

REQUIRED_FIELDS = {
    "cluxion_web_search": ["query"],
    "cluxion_browser_open": ["url"],
    "cluxion_browser_extract": [],
    "cluxion_browser_click": ["selector"],
    "cluxion_browser_type": ["selector", "text"],
}

PLAYWRIGHT_HINT = "pip install 'cluxion-agentplugin-preprocessing[browser]' && playwright install chromium"

_PUBLIC_FUNCTIONS = [
    browser_bridge.search,
    browser_bridge.open_url,
    browser_bridge.extract,
    browser_bridge.click,
    browser_bridge.type_text,
]


class FakeContext:
    def __init__(self) -> None:
        self.tools: dict[str, dict[str, object]] = {}

    def register_tool(
        self,
        *,
        name: str,
        toolset: str,
        schema: dict[str, object],
        handler: object,
        check_fn: object,
        emoji: str = "",
    ) -> None:
        self.tools[name] = {
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
            "check_fn": check_fn,
            "emoji": emoji,
        }


@pytest.mark.parametrize("tool_name", BROWSER_TOOL_NAMES)
def test_browser_tools_registered_with_schemas(tool_name: str) -> None:
    ctx = FakeContext()
    plugin.register(ctx)

    assert tool_name in ctx.tools
    assert ctx.tools[tool_name]["toolset"] == "cluxion"

    schema = SCHEMA_BY_NAME[tool_name]
    registered = ctx.tools[tool_name]["schema"]
    assert registered["name"] == schema["name"]
    assert registered["parameters"]["required"] == REQUIRED_FIELDS[tool_name]


def test_unknown_engine_without_playwright(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_bridge, "_import_playwright", lambda: None)
    result = browser_bridge.search("hello", engine="bing")
    assert result == {
        "ok": False,
        "error": "unknown_engine",
        "valid_engines": list(browser_bridge.VALID_ENGINES),
    }


def test_invalid_url_without_playwright(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_bridge, "_import_playwright", lambda: None)
    result = browser_bridge.open_url("ftp://example.com")
    assert result == {"ok": False, "error": "invalid_url"}


def test_selector_required_without_playwright(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_bridge, "_import_playwright", lambda: None)
    assert browser_bridge.click("") == {"ok": False, "error": "selector_required"}
    assert browser_bridge.type_text("", "secret") == {"ok": False, "error": "selector_required"}


@pytest.mark.parametrize("func", _PUBLIC_FUNCTIONS)
def test_playwright_missing_returns_hint(func: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_bridge, "_import_playwright", lambda: None)

    if func is browser_bridge.search:
        result = func("hello")
    elif func is browser_bridge.open_url:
        result = func("https://example.com")
    elif func is browser_bridge.extract:
        result = func()
    elif func is browser_bridge.click:
        result = func("button")
    else:
        result = func("input", "text")

    assert result["ok"] is False
    assert result["error"] == "playwright_not_installed"
    assert result["hint"] == PLAYWRIGHT_HINT


def test_shape_page_truncates_text_and_caps_links() -> None:
    links = [
        {"text": "A", "href": "https://example.com/a"},
        {"text": "B", "href": "https://example.com/a"},
        {"text": "C", "href": "https://example.com/c"},
        {"text": "D", "href": "mailto:nope@example.com"},
        {"text": "E", "href": "https://example.com/e"},
    ]
    shaped = browser_bridge._shape_page(
        title="Title",
        url="https://example.com/page",
        text="abcdefghij",
        links=links,
        max_chars=5,
        max_links=2,
        page_url="https://example.com/page",
        engine=None,
    )

    assert shaped["text"] == "abcde"
    assert shaped["truncated"] is True
    assert shaped["links"] == [
        {"text": "A", "href": "https://example.com/a"},
        {"text": "C", "href": "https://example.com/c"},
    ]


def test_shape_page_skips_engine_navigation_links() -> None:
    shaped = browser_bridge._shape_page(
        title="Google",
        url="https://www.google.com/search?q=cluxion",
        text="results",
        links=[
            {"text": "Images", "href": "https://www.google.com/imghp"},
            {"text": "Result", "href": "https://www.google.com/url?q=https://example.com"},
            {"text": "External", "href": "https://example.com/doc"},
        ],
        max_chars=8000,
        max_links=25,
        page_url="https://www.google.com/search?q=cluxion",
        engine="google",
    )

    assert shaped["links"] == [
        {"text": "Result", "href": "https://www.google.com/url?q=https://example.com"},
        {"text": "External", "href": "https://example.com/doc"},
    ]


@pytest.mark.skipif(
    importlib.util.find_spec("playwright") is None or os.environ.get("CLUXION_BROWSER_LIVE") != "1",
    reason="live browser test requires playwright and CLUXION_BROWSER_LIVE=1",
)
def test_live_search_smoke() -> None:
    result = browser_bridge.search("cluxion preprocessing", engine="duckduckgo", max_links=3, max_chars=500)
    assert result.get("ok") is True
    assert result.get("browser_mode")
    assert result.get("url")
    assert result.get("title")
