"""Tests for context compression stages 2 (LLM) and 3 (hybrid forgetting)."""

from __future__ import annotations

import subprocess

from cluxion_runtime.core import context_compress
from cluxion_runtime.core.context_compress import _Msg
from cluxion_runtime.core.hybrid_forget import _cold_demote, apply_hybrid_forget
from cluxion_runtime.core.preprocess import estimate_tokens


def _long(chars: int) -> str:
    return "x" * chars


def _pinned_heavy_payload() -> dict:
    """Stage 1 cannot reach target because intent and recent turns are huge and pinned."""
    return {
        "messages": [
            {"role": "user", "content": _long(6000)},
            {"role": "assistant", "content": _long(3000)},
            {"role": "tool", "content": _long(3000)},
            {"role": "assistant", "content": _long(3000)},
            {"role": "user", "content": _long(6000)},
        ],
        "context_limit_tokens": 3000,
        "keep_recent_turns": 1,
        "enable_llm_summary": True,
        "enable_forget": True,
    }


def test_stage2_summarizes_targets_and_preserves_pinned(monkeypatch) -> None:
    payload = _pinned_heavy_payload()
    stage1 = context_compress.compress({**payload, "enable_llm_summary": False, "enable_forget": False})
    assert stage1["tokens_after"] > stage1["context_limit"] * 0.30

    original_messages = [dict(m) for m in stage1["messages"]]
    pinned = set(stage1["pinned_indices"])

    def fake_summarize(messages, indices, instructions, *, model=None, timeout_s=120.0):
        return {idx: "ok" for idx in indices if idx not in pinned}

    monkeypatch.setattr(context_compress, "hermes_available", lambda: True)
    monkeypatch.setattr(context_compress, "summarize_messages", fake_summarize)

    result = context_compress.compress({**payload, "enable_forget": False})
    assert "llm_summary" in result["stages_applied"]
    assert result["tokens_after"] < stage1["tokens_after"]

    for idx, msg in enumerate(result["messages"]):
        if idx in pinned:
            assert msg["content"] == original_messages[idx]["content"]
        elif idx in {1, 2, 3}:
            assert msg["content"] == "ok"


def test_stage2_json_failure_falls_back_without_message_loss(monkeypatch) -> None:
    payload = _pinned_heavy_payload()
    stage1 = context_compress.compress({**payload, "enable_llm_summary": False, "enable_forget": False})

    monkeypatch.setattr(context_compress, "hermes_available", lambda: True)
    monkeypatch.setattr(context_compress, "summarize_messages", lambda *a, **k: None)

    result = context_compress.compress({**payload, "enable_forget": False})
    assert "llm_summary" not in result["stages_applied"]
    assert isinstance(result["ai_summary_request"], dict)
    assert len(result["messages"]) == len(stage1["messages"])
    assert [m["content"] for m in result["messages"]] == [m["content"] for m in stage1["messages"]]


def test_stage3_via_compress_drops_middle_messages(monkeypatch) -> None:
    payload = _pinned_heavy_payload()
    monkeypatch.setattr(context_compress, "hermes_available", lambda: False)

    result = context_compress.compress({**payload, "enable_llm_summary": False})
    assert "forget" in result["stages_applied"]
    assert len(result["messages"]) == 2
    assert result.get("over_target_pinned_only") is True


def test_cold_demote_returns_true_on_successful_store_only(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("cluxion_runtime.core.hybrid_forget.forgetforge_available", lambda: True)
    monkeypatch.setattr("cluxion_runtime.core.hybrid_forget.subprocess.run", fake_run)

    assert _cold_demote("recoverable content", "sess-abc123") is True
    assert len(calls) == 1
    assert calls[0] == ["forgetforge", "store", "sess-abc123", "--content", "recoverable content"]


def test_cold_demote_returns_false_when_store_fails(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr("cluxion_runtime.core.hybrid_forget.forgetforge_available", lambda: True)
    monkeypatch.setattr("cluxion_runtime.core.hybrid_forget.subprocess.run", fake_run)

    assert _cold_demote("content", "sess-fail") is False


def test_cold_demote_skips_subprocess_when_forgetforge_unavailable(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("cluxion_runtime.core.hybrid_forget.forgetforge_available", lambda: False)
    monkeypatch.setattr("cluxion_runtime.core.hybrid_forget.subprocess.run", fake_run)

    assert _cold_demote("content", "sess-none") is False
    assert calls == []


def test_stage3_sets_dropped_without_backup_for_recoverable(monkeypatch) -> None:
    monkeypatch.setattr("cluxion_runtime.core.hybrid_forget.forgetforge_available", lambda: False)
    messages = [
        _Msg("user", "intent", False),
        _Msg("assistant", _long(3000), False),
        _Msg("user", "recent", False),
    ]
    pinned = [0, 2]
    total = sum(estimate_tokens(m.content) for m in messages)
    result = apply_hybrid_forget(messages, pinned, total, 100)
    assert result.dropped_without_backup is True
    assert 1 in result.dropped_indices


def test_stage3_drops_low_importance_until_target() -> None:
    body = _long(4000)
    digest = f"[cluxion digest] tool: {body[:80]} [900 tokens elided]"
    messages = [
        _Msg("user", "intent: ship feature X at src/main.py", False),
        _Msg("assistant", body, False),
        _Msg("tool", digest, False),
        _Msg("assistant", "noise only", False),
        _Msg("user", "recent question", False),
    ]
    pinned = [0, 4]
    total = sum(estimate_tokens(m.content) for m in messages)
    target = 400

    result = apply_hybrid_forget(messages, pinned, total, target)
    assert result.tokens_after <= target
    assert 2 in result.dropped_indices
    assert result.messages[0].content.startswith("intent:")
    assert result.messages[-1].content == "recent question"


def test_stage3_prefers_junk_for_removal() -> None:
    body = _long(2000)
    messages = [
        _Msg("user", "intent", False),
        _Msg("assistant", "decision: keep JWT path src/auth.py", False),
        _Msg("tool", f"[cluxion digest] tool: {body[:80]} [500 tokens elided]", False),
        _Msg("assistant", body, False),
        _Msg("user", "latest", False),
    ]
    pinned = [0, 4]
    total = sum(estimate_tokens(m.content) for m in messages)
    target = total - estimate_tokens(messages[2].content) - 10

    result = apply_hybrid_forget(messages, pinned, total, target)
    assert 2 in result.dropped_indices


def test_over_target_pinned_only(monkeypatch) -> None:
    monkeypatch.setattr(context_compress, "hermes_available", lambda: False)
    result = context_compress.compress(
        {
            "messages": [
                {"role": "user", "content": _long(3000)},
                {"role": "assistant", "content": _long(3000)},
            ],
            "context_limit_tokens": 1000,
            "keep_recent_turns": 2,
            "enable_llm_summary": False,
            "enable_forget": True,
        }
    )
    assert result.get("over_target_pinned_only") is True
    assert result.get("forced_over_target") is True
    assert len(result["messages"]) == 2


def test_end_to_end_with_mocked_llm(monkeypatch) -> None:
    payload = _pinned_heavy_payload()

    def fake_summarize(messages, indices, instructions, *, model=None, timeout_s=120.0):
        return {idx: "s" for idx in indices}

    monkeypatch.setattr(context_compress, "hermes_available", lambda: True)
    monkeypatch.setattr(context_compress, "summarize_messages", fake_summarize)

    result = context_compress.compress(payload)
    target = int(0.30 * result["context_limit"])
    assert result["tokens_after"] <= target or result.get("over_target_pinned_only")


def test_llm_compress_parses_fenced_json() -> None:
    from cluxion_runtime.core import llm_compress

    stdout = 'Here is the result:\n```json\n{"1": "short summary"}\n```\n'
    parsed = llm_compress._parse_summary_json(stdout)
    assert parsed == {"1": "short summary"}


def test_summarize_messages_returns_none_on_bad_json(monkeypatch) -> None:
    from cluxion_runtime.core import llm_compress

    monkeypatch.setattr(llm_compress, "hermes_available", lambda: True)
    monkeypatch.setattr(llm_compress, "_call_hermes_oneshot", lambda *a, **k: "not json at all")
    assert llm_compress.summarize_messages([type("M", (), {"role": "user", "content": "hi"})()], [0]) is None


def _msg(content: str):
    return type("M", (), {"role": "user", "content": content})()


def test_hallucination_guard_strips_fabricated_port(monkeypatch) -> None:
    from cluxion_runtime.core import llm_compress

    source = "Connect to Redis on port 5433 for caching. File: recon_v4.py"
    llm_json = (
        '{"0": "Redis caching on port 5433. recon_v4.py. Hot: Redis:6390"}'
    )
    monkeypatch.setattr(llm_compress, "hermes_available", lambda: True)
    monkeypatch.setattr(llm_compress, "_call_hermes_oneshot", lambda *a, **k: llm_json)

    result = llm_compress.summarize_messages([_msg(source)], [0])
    assert result is not None
    assert "6390" not in result[0]
    assert "Hot:" not in result[0]
    assert "5433" in result[0]
    assert "recon_v4.py" in result[0]


def test_hallucination_guard_keeps_normalized_number(monkeypatch) -> None:
    from cluxion_runtime.core import llm_compress

    source = "Daily traffic is 482,000 requests with peak at 14 months uptime."
    llm_json = '{"0": "Traffic 482k requests, 14mo uptime."}'
    monkeypatch.setattr(llm_compress, "hermes_available", lambda: True)
    monkeypatch.setattr(llm_compress, "_call_hermes_oneshot", lambda *a, **k: llm_json)

    result = llm_compress.summarize_messages([_msg(source)], [0])
    assert result is not None
    assert "482k" in result[0]
    assert "14mo" in result[0]


def test_hallucination_guard_keeps_korean_normalized_number(monkeypatch) -> None:
    from cluxion_runtime.core import llm_compress

    source = "일평균 482,000건 처리, Redis 포트 5433 사용."
    llm_json = '{"0": "일평균 482k/day, Redis 5433."}'
    monkeypatch.setattr(llm_compress, "hermes_available", lambda: True)
    monkeypatch.setattr(llm_compress, "_call_hermes_oneshot", lambda *a, **k: llm_json)

    result = llm_compress.summarize_messages([_msg(source)], [0])
    assert result is not None
    assert "482k" in result[0]
    assert "5433" in result[0]


def test_hallucination_guard_all_fabricated_returns_none(monkeypatch) -> None:
    from cluxion_runtime.core import llm_compress

    source = "Discuss caching strategy for the API layer."
    llm_json = '{"0": "Hot: Redis:6390"}'
    monkeypatch.setattr(llm_compress, "hermes_available", lambda: True)
    monkeypatch.setattr(llm_compress, "_call_hermes_oneshot", lambda *a, **k: llm_json)

    assert llm_compress.summarize_messages([_msg(source)], [0]) is None


def test_pinned_recent_last_resort_brings_under_target(monkeypatch) -> None:
    """Live edge case: all messages pinned and huge — intent preserved, usage <= target."""
    monkeypatch.setattr(context_compress, "hermes_available", lambda: False)
    intent = "TASK_INTENT: implement pinned-overflow guard"
    payload = {
        "messages": [
            {"role": "user", "content": intent + _long(160_000)},
            {"role": "assistant", "content": _long(160_000)},
            {"role": "tool", "content": _long(160_000)},
            {"role": "assistant", "content": _long(160_000)},
            {"role": "user", "content": _long(160_000)},
        ],
        # 5 x ~40k tokens ~ 200k total -> usage 0.80 at this limit (live edge case).
        "context_limit_tokens": 250_000,
        "keep_recent_turns": 4,
        "enable_llm_summary": False,
        "enable_forget": True,
    }
    result = context_compress.compress(payload)
    target = int(0.30 * result["context_limit"])
    assert result["usage_before"] >= 0.70
    assert result["messages"][0]["content"].startswith(intent)
    assert result["tokens_after"] <= target
    assert result.get("over_target_pinned_only") is not True
    assert "truncate_pinned_recent" in result["stages_applied"]


def test_lone_giant_intent_forced_over_target(monkeypatch) -> None:
    """When intent alone exceeds target, truncate everything else and flag forced_over_target."""
    monkeypatch.setattr(context_compress, "hermes_available", lambda: False)
    intent = "GIANT_INTENT"
    payload = {
        "messages": [
            {"role": "user", "content": intent + _long(12_000)},
            {"role": "assistant", "content": _long(12_000)},
        ],
        "context_limit_tokens": 1000,
        "keep_recent_turns": 2,
        "enable_llm_summary": False,
        "enable_forget": True,
    }
    result = context_compress.compress(payload)
    assert result["messages"][0]["content"].startswith(intent)
    assert result.get("forced_over_target") is True
    assert result.get("over_target_pinned_only") is True
    assert "[...cluxion:" in result["messages"][1]["content"]
    assert result["tokens_after"] > int(0.30 * result["context_limit"])


def test_korean_decision_survives_stage3() -> None:
    body = _long(4000)
    digest = f"[cluxion digest] tool: {body[:80]} [900 tokens elided]"
    messages = [
        _Msg("user", "의도: 기능 X 구현", False),
        _Msg("assistant", body, False),
        _Msg("tool", digest, False),
        _Msg("assistant", "noise only", False),
        _Msg("user", "결정: JWT 경로 src/auth.py 유지", False),
        _Msg("user", "latest", False),
    ]
    pinned = [0, 5]
    total = sum(estimate_tokens(m.content) for m in messages)
    target = 400

    result = apply_hybrid_forget(messages, pinned, total, target)
    assert result.tokens_after <= target
    assert 2 in result.dropped_indices
    assert 4 not in result.dropped_indices
    assert any("결정" in msg.content for msg in result.messages)
    assert result.messages[0].content.startswith("의도:")