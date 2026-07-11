"""Browser bridge unit tests — no live browser or Playwright install required."""

from __future__ import annotations

import importlib
import os
import signal
from unittest.mock import MagicMock

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


def test_cdp_sequence_reuses_bridge_page_until_session_teardown(monkeypatch: pytest.MonkeyPatch) -> None:
    user_page = MagicMock()
    user_page.url = "https://example.com/user-work"
    bridge_page = MagicMock()
    bridge_page.url = "https://example.com/bridge"
    bridge_page.title.return_value = "Title"
    bridge_page.is_closed.return_value = False
    bridge_page.evaluate.side_effect = ["Body", [], "After click", []]
    locator = bridge_page.locator.return_value
    locator.count.return_value = 1
    context = MagicMock()
    context.pages = [user_page]
    context.new_page.return_value = bridge_page
    browser = MagicMock()
    browser.contexts = [context]
    monkeypatch.setattr(browser_bridge, "_import_playwright", lambda: object())
    monkeypatch.setattr(browser_bridge.time, "sleep", lambda _: None)
    monkeypatch.setitem(browser_bridge._session, "playwright", MagicMock())
    monkeypatch.setitem(browser_bridge._session, "browser", browser)
    monkeypatch.setitem(browser_bridge._session, "context", None)
    monkeypatch.setitem(browser_bridge._session, "mode", "cdp")
    monkeypatch.setitem(browser_bridge._session, "page", None)

    open_result = browser_bridge.open_url("https://example.com/bridge")
    click_result = browser_bridge.click("#next")
    extract_result = browser_bridge.extract()

    assert open_result["ok"] is True
    assert click_result["ok"] is True
    assert extract_result["ok"] is True
    context.new_page.assert_called_once_with()
    assert browser_bridge._session["page"] is bridge_page
    bridge_page.goto.assert_called_once_with(
        "https://example.com/bridge", wait_until="domcontentloaded", timeout=browser_bridge._NAVIGATE_TIMEOUT_MS
    )
    bridge_page.locator.assert_called_once_with("#next")
    locator.first.click.assert_called_once_with(timeout=browser_bridge._CLICK_TIMEOUT_MS)
    bridge_page.close.assert_not_called()
    user_page.goto.assert_not_called()
    user_page.close.assert_not_called()
    context.close.assert_not_called()
    browser.close.assert_not_called()

    browser_bridge._close_session()
    browser_bridge._close_session()

    bridge_page.close.assert_called_once_with()
    assert browser_bridge._session["page"] is None
    user_page.close.assert_not_called()
    context.close.assert_not_called()
    browser.close.assert_not_called()


def test_profile_mode_never_uses_or_closes_preexisting_page(monkeypatch: pytest.MonkeyPatch) -> None:
    user_page = MagicMock()
    user_page.url = "https://example.com/restored"
    bridge_page = MagicMock()
    bridge_page.url = "https://example.com/bridge"
    bridge_page.title.return_value = "Title"
    bridge_page.is_closed.return_value = False
    bridge_page.evaluate.side_effect = ["Body", []]
    context = MagicMock()
    context.pages = [user_page]
    context.new_page.return_value = bridge_page
    monkeypatch.setattr(browser_bridge, "_import_playwright", lambda: object())
    monkeypatch.setattr(browser_bridge.time, "sleep", lambda _: None)
    monkeypatch.setitem(browser_bridge._session, "playwright", MagicMock())
    monkeypatch.setitem(browser_bridge._session, "browser", None)
    monkeypatch.setitem(browser_bridge._session, "context", context)
    monkeypatch.setitem(browser_bridge._session, "mode", "chrome-profile")
    monkeypatch.setitem(browser_bridge._session, "page", None)

    result = browser_bridge.open_url("https://example.com/bridge")

    assert result["ok"] is True
    context.new_page.assert_called_once_with()
    assert browser_bridge._session["page"] is bridge_page
    bridge_page.goto.assert_called_once_with(
        "https://example.com/bridge", wait_until="domcontentloaded", timeout=browser_bridge._NAVIGATE_TIMEOUT_MS
    )
    bridge_page.close.assert_not_called()
    user_page.goto.assert_not_called()
    user_page.close.assert_not_called()

    browser_bridge._close_session()

    bridge_page.close.assert_called_once_with()
    assert browser_bridge._session["page"] is None
    user_page.close.assert_not_called()
    context.close.assert_called_once_with()


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


# --- Cycle 108: SIGTERM prior chaining + session RLock ---


def test_sigterm_handler_calls_callable_prior_after_close(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[str] = []
    prior_calls: list[tuple[int, object]] = []

    def prior(signum: int, frame: object) -> None:
        prior_calls.append((signum, frame))

    monkeypatch.setattr(browser_bridge, "_previous_sigterm_handler", prior)
    monkeypatch.setattr(browser_bridge, "_close_session", lambda: closed.append("closed"))

    browser_bridge._handle_sigterm(signal.SIGTERM, None)

    assert closed == ["closed"]
    assert prior_calls == [(signal.SIGTERM, None)]


def test_sigterm_handler_sig_dfl_raises_system_exit_143(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[str] = []
    monkeypatch.setattr(browser_bridge, "_previous_sigterm_handler", signal.SIG_DFL)
    monkeypatch.setattr(browser_bridge, "_close_session", lambda: closed.append("closed"))

    with pytest.raises(SystemExit) as excinfo:
        browser_bridge._handle_sigterm(signal.SIGTERM, None)

    assert closed == ["closed"]
    assert excinfo.value.code == 143


def test_sigterm_handler_sig_ign_returns_after_close(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[str] = []
    monkeypatch.setattr(browser_bridge, "_previous_sigterm_handler", signal.SIG_IGN)
    monkeypatch.setattr(browser_bridge, "_close_session", lambda: closed.append("closed"))

    assert browser_bridge._handle_sigterm(signal.SIGTERM, None) is None
    assert closed == ["closed"]


def test_concurrent_cold_ensure_single_playwright(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two-thread cold ensure creates exactly one Playwright start."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    starts: list[int] = []

    class _Chromium:
        def connect_over_cdp(self, _endpoint: str):
            raise RuntimeError("cdp down")

        def launch_persistent_context(self, **_kwargs):
            return MagicMock()

    def fake_sync_playwright():
        class _Factory:
            def start(self):
                starts.append(1)
                time.sleep(0.08)  # widen the race window without dual-entry barriers
                pw = MagicMock()
                pw.chromium = _Chromium()
                return pw

        return _Factory()

    browser_bridge._close_session()
    monkeypatch.setattr(browser_bridge, "_import_playwright", lambda: fake_sync_playwright)
    monkeypatch.setenv("CLUXION_BROWSER_CDP", "")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(browser_bridge._ensure_session) for _ in range(2)]
        results = [f.result(timeout=5) for f in futures]

    assert len(starts) == 1, f"expected one Playwright start, got {len(starts)}"
    assert all(r.get("ok") is True for r in results)
    browser_bridge._close_session()


def test_close_vs_init_serializes(monkeypatch: pytest.MonkeyPatch) -> None:
    """close-vs-init race: RLock serializes so close cannot tear mid-create."""
    import threading
    import time

    events: list[str] = []
    events_lock = threading.Lock()

    def note(label: str) -> None:
        with events_lock:
            events.append(label)

    class _Chromium:
        def connect_over_cdp(self, _endpoint: str):
            raise RuntimeError("cdp down")

        def launch_persistent_context(self, **_kwargs):
            return MagicMock()

    def fake_sync_playwright():
        class _Factory:
            def start(self):
                note("start")
                time.sleep(0.12)
                pw = MagicMock()
                pw.chromium = _Chromium()
                note("created")
                return pw

        return _Factory()

    browser_bridge._close_session()
    monkeypatch.setattr(browser_bridge, "_import_playwright", lambda: fake_sync_playwright)
    monkeypatch.setenv("CLUXION_BROWSER_CDP", "")

    errors: list[BaseException] = []

    def ensure_worker() -> None:
        try:
            browser_bridge._ensure_session()
            note("ensure_done")
        except BaseException as exc:
            errors.append(exc)

    def close_worker() -> None:
        time.sleep(0.03)  # let ensure enter create when racing
        browser_bridge._close_session()
        note("close_done")

    t1 = threading.Thread(target=ensure_worker)
    t2 = threading.Thread(target=close_worker)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not errors, errors
    assert "start" in events and "created" in events and "close_done" in events
    start_i = events.index("start")
    created_i = events.index("created")
    close_i = events.index("close_done")
    assert not (start_i < close_i < created_i), f"close interleaved mid-init: {events}"
    browser_bridge._close_session()


def test_sigterm_reinstall_preserves_original_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """A module reload/reinstall must unwrap our old handler instead of chaining to itself."""
    original_calls: list[int] = []
    installed: list[object] = []

    def original(signum: int, _frame: object) -> None:
        original_calls.append(signum)

    def old_cluxion_handler(_signum: int, _frame: object) -> None:
        raise AssertionError("old handler must be unwrapped, not chained")

    old_cluxion_handler._cluxion_browser_sigterm = True
    old_cluxion_handler._cluxion_previous_sigterm = original
    monkeypatch.setattr(browser_bridge.signal, "getsignal", lambda _sig: old_cluxion_handler)
    monkeypatch.setattr(browser_bridge.signal, "signal", lambda _sig, handler: installed.append(handler))
    monkeypatch.setattr(browser_bridge, "_close_session", lambda: None)

    browser_bridge._install_sigterm_handler()
    installed[-1](signal.SIGTERM, None)

    assert original_calls == [signal.SIGTERM]
    assert browser_bridge._previous_sigterm_handler is original


def test_with_page_serializes_callback_against_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """A close in another thread cannot invalidate the shared page during a callback."""
    import threading
    import time

    entered = threading.Event()
    release = threading.Event()
    close_done = threading.Event()
    page = MagicMock()
    monkeypatch.setattr(browser_bridge, "_import_playwright", lambda: object())
    monkeypatch.setattr(browser_bridge, "_get_page", lambda: (page, "test"))
    monkeypatch.setattr(browser_bridge, "_close_session_unlocked", close_done.set)

    def callback(_page, _mode):
        entered.set()
        assert release.wait(timeout=2)
        assert not close_done.is_set()
        return {"ok": True}

    worker = threading.Thread(target=browser_bridge._with_page, args=(callback,))
    closer = threading.Thread(target=lambda: (entered.wait(timeout=2), browser_bridge._close_session()))
    worker.start()
    closer.start()
    assert entered.wait(timeout=2)
    time.sleep(0.05)
    assert not close_done.is_set()
    release.set()
    worker.join(timeout=2); closer.join(timeout=2)
    assert close_done.is_set()
