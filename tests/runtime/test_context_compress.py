"""Parity tests: context compression must behave identically on all backends."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

from cluxion_runtime.core import context_compress
from cluxion_runtime.resources import queue_bridge

_LOCAL_BIN = Path(__file__).resolve().parents[2] / "rust" / "cluxion_queue" / "target" / "release" / "cluxion-queue"

BACKENDS = ["python"]
if importlib.util.find_spec("cluxion_queue_native") is not None:
    BACKENDS.append("native")
if _LOCAL_BIN.exists() or shutil.which("cluxion-queue"):
    BACKENDS.append("subprocess")


@pytest.fixture(params=BACKENDS)
def backend(request, monkeypatch):
    monkeypatch.setenv(queue_bridge.QUEUE_BACKEND_ENV, request.param)
    if request.param == "subprocess" and _LOCAL_BIN.exists():
        monkeypatch.setenv(queue_bridge.QUEUE_BIN_ENV, str(_LOCAL_BIN))
    return request.param


def _long(chars: int) -> str:
    return "x" * chars


_COMPRESSIBLE = {
    "messages": [
        {"role": "user", "content": "the task intent"},
        {"role": "assistant", "content": _long(4000)},
        {"role": "tool", "content": _long(4000)},
        {"role": "assistant", "content": "duplicate body " + _long(50)},
        {"role": "tool", "content": "duplicate body " + _long(50)},
        {"role": "assistant", "content": _long(4000)},
        {"role": "user", "content": "recent question"},
    ],
    "context_limit_tokens": 3000,
    "keep_recent_turns": 1,
}


def test_noop_below_trigger(backend) -> None:
    result = queue_bridge.compress_context(
        {"messages": [{"role": "user", "content": "hello"}], "context_limit_tokens": 1000}
    )
    assert result["ok"] is True
    assert result["compressed"] is False
    assert result["stages_applied"] == []
    assert result["tokens_before"] == result["tokens_after"]
    assert result["messages"][0]["content"] == "hello"


def test_compresses_and_preserves_pinned(backend) -> None:
    result = queue_bridge.compress_context(_COMPRESSIBLE)
    assert result["ok"] is True
    assert result["compressed"] is True
    assert result["tokens_after"] < result["tokens_before"]
    messages = result["messages"]
    # first user message (intent) and the most recent turn stay untouched
    assert messages[0]["content"] == "the task intent"
    assert messages[-1]["content"] == "recent question"
    assert 0 in result["pinned_indices"]
    assert len(_COMPRESSIBLE["messages"]) - 1 in result["pinned_indices"]


def test_explicit_pinned_never_compressed(backend) -> None:
    body = _long(4000)
    result = queue_bridge.compress_context(
        {
            "messages": [
                {"role": "user", "content": "intent"},
                {"role": "assistant", "content": body, "pinned": True},
                {"role": "tool", "content": body},
                {"role": "user", "content": "now"},
            ],
            "context_limit_tokens": 2000,
            "keep_recent_turns": 1,
        }
    )
    assert result["messages"][1]["content"] == body
    assert result["messages"][2]["content"] != body


def test_all_pinned_requests_ai_summary(backend) -> None:
    result = queue_bridge.compress_context(
        {
            "messages": [
                {"role": "user", "content": _long(3000)},
                {"role": "assistant", "content": _long(3000)},
            ],
            "context_limit_tokens": 1000,
            "keep_recent_turns": 2,
        }
    )
    request = result["ai_summary_request"]
    assert isinstance(request, dict)
    assert request["target_tokens"] == 300
    assert request["summarize_indices"] == []


def test_model_registry_resolution(backend) -> None:
    result = queue_bridge.compress_context({"messages": [{"role": "user", "content": "hi"}], "model": "Claude-Fable-5"})
    assert result["context_limit"] == 200_000


def test_backend_matches_python_reference(backend) -> None:
    reference = context_compress.compress(dict(_COMPRESSIBLE))
    result = queue_bridge.compress_context(_COMPRESSIBLE)
    assert result == reference


def test_missing_messages_raises(backend) -> None:
    with pytest.raises(RuntimeError, match="messages"):
        queue_bridge.compress_context({"context_limit_tokens": 1000})
