"""Playwright sync bridge to the user's Chrome session for web search and navigation.

Connection order: CDP attach to running Chrome, else a dedicated persistent Chrome
profile, else headless Chromium. Playwright is optional; every public entry point
returns a dict and never raises to callers.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import signal
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urldefrag, urlparse

if TYPE_CHECKING:
    from collections.abc import Sequence

PLAYWRIGHT_HINT = "pip install 'cluxion-agentplugin-preprocessing[browser]' && playwright install chromium"
BROWSER_HINT = "start Chrome with --remote-debugging-port=9222 or install playwright browsers"

VALID_ENGINES = ("google", "naver", "duckduckgo", "perplexity")
ENGINE_URLS: dict[str, str] = {
    "google": "https://www.google.com/search?q={q}",
    "naver": "https://search.naver.com/search.naver?query={q}",
    "duckduckgo": "https://duckduckgo.com/?q={q}",
    "perplexity": "https://www.perplexity.ai/search?q={q}",
}
_ENGINE_NAV_PREFIXES: dict[str, tuple[str, ...]] = {
    "google": ("https://www.google.com/", "https://google.com/"),
    "naver": ("https://search.naver.com/", "https://www.naver.com/"),
    "duckduckgo": ("https://duckduckgo.com/",),
    "perplexity": ("https://www.perplexity.ai/",),
}

_NAVIGATE_TIMEOUT_MS = 15_000
_SETTLE_SECONDS = 0.35
_CLICK_TIMEOUT_MS = 5_000

_session: dict[str, Any] = {
    "playwright": None,
    "browser": None,
    "context": None,
    "mode": None,
}


def search(
    query: str,
    *,
    engine: str = "google",
    max_links: int = 25,
    max_chars: int = 8000,
) -> dict[str, Any]:
    """Run a web search in the user's browser and return generic page extraction."""
    engine_key = str(engine).strip().lower()
    if engine_key not in ENGINE_URLS:
        return {
            "ok": False,
            "error": "unknown_engine",
            "valid_engines": list(VALID_ENGINES),
        }
    query_text = str(query).strip()
    if not query_text:
        return {"ok": False, "error": "query_required"}

    url = ENGINE_URLS[engine_key].format(q=quote(query_text))
    return _navigate_and_extract(url, max_chars=max_chars, max_links=max_links, engine=engine_key)


def open_url(url: str, *, max_chars: int = 8000) -> dict[str, Any]:
    """Navigate the current browser tab to a URL and return generic extraction."""
    target = str(url).strip()
    if not _is_valid_http_url(target):
        return {"ok": False, "error": "invalid_url"}
    return _navigate_and_extract(target, max_chars=max_chars, max_links=25, engine=None)


def extract(*, selector: str | None = None, max_chars: int = 8000) -> dict[str, Any]:
    """Extract text from the current page, optionally scoped to a CSS selector."""
    selector_text = str(selector).strip() if selector is not None else ""

    def _do_extract(page: Any, mode: str) -> dict[str, Any]:
        if selector_text:
            locator = page.locator(selector_text)
            if locator.count() == 0:
                return {"ok": False, "error": "selector_not_found", "browser_mode": mode}
            text = locator.first.inner_text(timeout=_CLICK_TIMEOUT_MS)
            shaped = _shape_page(
                title=page.title(),
                url=page.url,
                text=text,
                links=[],
                max_chars=max_chars,
                max_links=0,
                page_url=page.url,
                engine=None,
            )
        else:
            shaped = _extract_page(page, max_chars=max_chars, max_links=25, engine=None)
        shaped["ok"] = True
        shaped["browser_mode"] = mode
        return shaped

    return _with_page(_do_extract)


def click(selector: str) -> dict[str, Any]:
    """Click the first element matching selector and return the resulting page metadata."""
    selector_text = str(selector).strip()
    if not selector_text:
        return {"ok": False, "error": "selector_required"}

    def _do_click(page: Any, mode: str) -> dict[str, Any]:
        locator = page.locator(selector_text)
        if locator.count() == 0:
            return {"ok": False, "error": "selector_not_found", "browser_mode": mode}
        locator.first.click(timeout=_CLICK_TIMEOUT_MS)
        time.sleep(_SETTLE_SECONDS)
        return {
            "ok": True,
            "browser_mode": mode,
            "url": page.url,
            "title": page.title(),
        }

    return _with_page(_do_click)


def type_text(selector: str, text: str, *, submit: bool = False) -> dict[str, Any]:
    """Fill the first element matching selector; optionally press Enter to submit."""
    selector_text = str(selector).strip()
    if not selector_text:
        return {"ok": False, "error": "selector_required"}
    if text is None:
        return {"ok": False, "error": "text_required"}

    def _do_type(page: Any, mode: str) -> dict[str, Any]:
        locator = page.locator(selector_text)
        if locator.count() == 0:
            return {"ok": False, "error": "selector_not_found", "browser_mode": mode}
        target = locator.first
        target.fill(str(text), timeout=_CLICK_TIMEOUT_MS)
        if submit:
            target.press("Enter", timeout=_CLICK_TIMEOUT_MS)
            time.sleep(_SETTLE_SECONDS)
        return {
            "ok": True,
            "browser_mode": mode,
            "url": page.url,
            "title": page.title(),
        }

    return _with_page(_do_type)


def _shape_page(
    *,
    title: str,
    url: str,
    text: str,
    links: Sequence[dict[str, str]],
    max_chars: int,
    max_links: int,
    page_url: str,
    engine: str | None,
) -> dict[str, Any]:
    """Pure helper: cap text, dedupe links, and skip non-http or engine-nav URLs."""
    body = str(text)
    truncated = False
    if max_chars >= 0 and len(body) > max_chars:
        body = body[:max_chars]
        truncated = True

    shaped_links: list[dict[str, str]] = []
    seen_hrefs: set[str] = set()
    for raw in links:
        href = str(raw.get("href", "")).strip()
        link_text = str(raw.get("text", "")).strip()
        if _should_skip_link(href, page_url=page_url, engine=engine):
            continue
        if href in seen_hrefs:
            continue
        seen_hrefs.add(href)
        shaped_links.append({"text": link_text, "href": href})
        if len(shaped_links) >= max_links:
            break

    result: dict[str, Any] = {"title": title, "url": url, "text": body, "links": shaped_links}
    if truncated:
        result["truncated"] = True
    return result


def _should_skip_link(href: str, *, page_url: str, engine: str | None) -> bool:
    if not href or href.startswith("#"):
        return True
    if not href.startswith(("http://", "https://")):
        return True
    href_base, _ = urldefrag(href)
    page_base, _ = urldefrag(page_url)
    if href_base == page_base:
        return True
    return bool(engine and _is_engine_nav_link(href, engine))


def _is_engine_nav_link(href: str, engine: str) -> bool:
    prefixes = _ENGINE_NAV_PREFIXES.get(engine, ())
    if not any(href.startswith(prefix) for prefix in prefixes):
        return False
    return not (engine == "google" and "/url?" in href)


def _is_valid_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _playwright_not_installed() -> dict[str, Any]:
    return {"ok": False, "error": "playwright_not_installed", "hint": PLAYWRIGHT_HINT}


def _browser_unreachable(mode: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "error": "browser_unreachable", "hint": BROWSER_HINT}
    if mode:
        payload["browser_mode"] = mode
    return payload


def _import_playwright() -> Any | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    return sync_playwright


def _ensure_session() -> dict[str, Any]:
    if _session["mode"] is not None:
        return {"ok": True, "browser_mode": _session["mode"]}

    sync_playwright = _import_playwright()
    if sync_playwright is None:
        return _playwright_not_installed()

    try:
        playwright = sync_playwright().start()
        _session["playwright"] = playwright

        cdp_endpoint = os.environ.get("CLUXION_BROWSER_CDP", "http://127.0.0.1:9222").strip()
        if cdp_endpoint:
            try:
                browser = playwright.chromium.connect_over_cdp(cdp_endpoint)
                _session["browser"] = browser
                _session["mode"] = "cdp"
                return {"ok": True, "browser_mode": "cdp"}
            except Exception:
                pass

        profile_dir = Path.home() / ".cluxion" / "browser-profile"
        profile_dir.mkdir(parents=True, exist_ok=True)

        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                channel="chrome",
                headless=False,
            )
            _session["context"] = context
            _session["mode"] = "chrome-profile"
            return {"ok": True, "browser_mode": "chrome-profile"}
        except Exception:
            try:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=True,
                )
                _session["context"] = context
                _session["mode"] = "chromium-headless"
                return {"ok": True, "browser_mode": "chromium-headless"}
            except Exception:
                _close_session()
                return _browser_unreachable()
    except Exception:
        _close_session()
        return _browser_unreachable()


def _get_page() -> tuple[Any | None, str | None]:
    session = _ensure_session()
    if not session.get("ok"):
        return None, None

    mode = str(session["browser_mode"])
    try:
        browser = _session.get("browser")
        if browser is not None:
            contexts = browser.contexts
            if contexts:
                context = contexts[0]
                if context.pages:
                    return context.pages[0], mode
                return context.new_page(), mode
            context = browser.new_context()
            return context.new_page(), mode

        context = _session.get("context")
        if context is not None:
            if context.pages:
                return context.pages[0], mode
            return context.new_page(), mode
    except Exception:
        # Cached session whose browser/CDP link died: reset so the next call reconnects.
        _close_session()
        return None, None

    return None, mode


def _with_page(callback: Any) -> dict[str, Any]:
    sync_playwright = _import_playwright()
    if sync_playwright is None:
        return _playwright_not_installed()

    page, mode = _get_page()
    if page is None or mode is None:
        session = _ensure_session()
        if not session.get("ok"):
            return session
        return _browser_unreachable(session.get("browser_mode"))

    try:
        result = callback(page, mode)
        if isinstance(result, dict):
            if result.get("ok") and "browser_mode" not in result:
                result["browser_mode"] = mode
            return result
        return _browser_unreachable(mode)
    except Exception:
        return _browser_unreachable(mode)


def _navigate_and_extract(
    url: str,
    *,
    max_chars: int,
    max_links: int,
    engine: str | None,
) -> dict[str, Any]:
    def _do_navigate(page: Any, mode: str) -> dict[str, Any]:
        page.goto(url, wait_until="domcontentloaded", timeout=_NAVIGATE_TIMEOUT_MS)
        time.sleep(_SETTLE_SECONDS)
        shaped = _extract_page(page, max_chars=max_chars, max_links=max_links, engine=engine)
        shaped["ok"] = True
        shaped["browser_mode"] = mode
        return shaped

    return _with_page(_do_navigate)


def _extract_page(page: Any, *, max_chars: int, max_links: int, engine: str | None) -> dict[str, Any]:
    title = page.title()
    current_url = page.url
    text = page.evaluate("() => (document.body && document.body.innerText) ? document.body.innerText : ''")
    raw_links = page.evaluate(
        """() => Array.from(document.querySelectorAll('a[href]')).map((anchor) => ({
            text: (anchor.innerText || '').trim(),
            href: anchor.href
        }))"""
    )
    if not isinstance(raw_links, list):
        raw_links = []
    return _shape_page(
        title=title,
        url=current_url,
        text=str(text),
        links=raw_links,
        max_chars=max_chars,
        max_links=max_links,
        page_url=current_url,
        engine=engine,
    )


def _close_session() -> None:
    context = _session.get("context")
    browser = _session.get("browser")
    playwright = _session.get("playwright")

    if context is not None:
        with contextlib.suppress(Exception):
            context.close()
    if browser is not None:
        with contextlib.suppress(Exception):
            browser.close()
    if playwright is not None:
        with contextlib.suppress(Exception):
            playwright.stop()

    _session["playwright"] = None
    _session["browser"] = None
    _session["context"] = None
    _session["mode"] = None


def _handle_sigterm(signum: int, _frame: object) -> None:
    # SIGKILL is uncatchable; SIGTERM cleanup is best-effort before exit.
    _close_session()
    raise SystemExit(128 + signum)


atexit.register(_close_session)
with contextlib.suppress(AttributeError, OSError, ValueError):
    signal.signal(signal.SIGTERM, _handle_sigterm)


__all__ = [
    "_shape_page",
    "click",
    "extract",
    "open_url",
    "search",
    "type_text",
]
