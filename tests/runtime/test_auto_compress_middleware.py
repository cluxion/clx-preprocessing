"""Tests for llm_request auto-compress middleware."""

from __future__ import annotations

from cluxion_agentplugin_preprocessing import plugin
from cluxion_runtime.core.preprocess import estimate_tokens


def _long(chars: int) -> str:
    return "x" * chars


def _heavy_messages() -> list[dict[str, str]]:
    return [
        {"role": "user", "content": "task intent"},
        *[{"role": "assistant", "content": _long(4000)} for _ in range(6)],
        {"role": "user", "content": "recent"},
    ]


def test_middleware_noop_below_trigger() -> None:
    request = {"messages": [{"role": "user", "content": "hello"}]}
    assert plugin._auto_compress_middleware(request, model="gpt-4") is None


def test_middleware_compresses_at_or_above_trigger(monkeypatch) -> None:
    monkeypatch.setattr(
        "cluxion_runtime.core.context_compress.hermes_available",
        lambda: False,
    )
    messages = _heavy_messages()
    request = {"messages": messages, "context_limit_tokens": 3000}
    before = sum(estimate_tokens(m["content"]) for m in messages)

    result = plugin._auto_compress_middleware(request, model="gpt-4")
    assert result is not None
    assert result["source"] == "preprocessing"
    shrunk = result["request"]["messages"]
    assert isinstance(shrunk, list)
    after = sum(estimate_tokens(m["content"]) for m in shrunk)
    assert after < before
    assert shrunk[0]["content"] == "task intent"
    assert shrunk[-1]["content"] == "recent"


def test_middleware_recursion_guard_returns_none(monkeypatch) -> None:
    monkeypatch.setenv("CLUXION_PREPROCESS_IN_COMPRESS", "1")
    request = {"messages": _heavy_messages()}
    assert plugin._auto_compress_middleware(request, model="gpt-4") is None


def test_middleware_autocompress_disabled_returns_none(monkeypatch) -> None:
    monkeypatch.setenv("CLUXION_PREPROCESS_AUTOCOMPRESS", "0")
    request = {"messages": _heavy_messages()}
    assert plugin._auto_compress_middleware(request, model="gpt-4") is None


def test_middleware_compress_exception_returns_none(monkeypatch) -> None:
    def boom(_payload):
        raise RuntimeError("compress failed")

    monkeypatch.setattr(plugin, "compress", boom)
    request = {"messages": _heavy_messages()}
    assert plugin._auto_compress_middleware(request, model="gpt-4") is None


def test_middleware_missing_messages_returns_none() -> None:
    assert plugin._auto_compress_middleware({"model": "gpt-4"}) is None


def test_middleware_codex_input_key(monkeypatch) -> None:
    monkeypatch.setattr(
        "cluxion_runtime.core.context_compress.hermes_available",
        lambda: False,
    )
    messages = _heavy_messages()
    request = {"input": messages, "context_limit_tokens": 3000}
    result = plugin._auto_compress_middleware(request, model="gpt-4")
    assert result is not None
    assert "input" in result["request"]
    assert "messages" not in result["request"]


def test_register_skips_middleware_when_unsupported() -> None:
    class FakeContext:
        def __init__(self) -> None:
            self.tools: dict[str, object] = {}
            self.middleware: list[tuple[str, object]] = []

        def register_tool(self, **kwargs: object) -> None:
            self.tools[str(kwargs["name"])] = kwargs

        def register_hook(self, *_: object) -> None:
            pass

    ctx = FakeContext()
    plugin.register(ctx)
    assert ctx.middleware == []


def test_register_middleware_when_supported() -> None:
    class FakeContext:
        def __init__(self) -> None:
            self.tools: dict[str, object] = {}
            self.middleware: list[tuple[str, object]] = []

        def register_tool(self, **kwargs: object) -> None:
            self.tools[str(kwargs["name"])] = kwargs

        def register_hook(self, *_: object) -> None:
            pass

        def register_middleware(self, name: str, handler: object) -> None:
            self.middleware.append((name, handler))

    ctx = FakeContext()
    plugin.register(ctx)
    assert ctx.middleware == [("llm_request", plugin._auto_compress_middleware)]