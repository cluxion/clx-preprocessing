"""Byte-identical parity tests for estimate_tokens optimization."""

from __future__ import annotations

import random

from cluxion_runtime.core.preprocess import estimate_tokens


def _estimate_tokens_legacy(text: str) -> int:
    cjk = sum(1 for ch in text if ord(ch) > 127)
    ascii_chars = max(0, len(text) - cjk)
    return max(1, cjk + ascii_chars // 4)


def test_estimate_tokens_matches_legacy_fixed_cases() -> None:
    cases = [
        ("", 1),
        ("abcd", 1),
        ("한글텍스트", 5),
        ("ab한", 1),
    ]
    for text, expected in cases:
        assert estimate_tokens(text) == expected
        assert estimate_tokens(text) == _estimate_tokens_legacy(text)


def test_estimate_tokens_matches_legacy_random_unicode() -> None:
    rng = random.Random(0)
    chars: list[str] = []
    for _ in range(10_000):
        roll = rng.random()
        if roll < 0.4:
            chars.append(chr(rng.randint(0, 127)))
        elif roll < 0.7:
            chars.append(chr(rng.randint(128, 0xFFFF)))
        else:
            chars.append(chr(rng.randint(0x10000, 0x10FFFF)))
    text = "".join(chars)
    assert estimate_tokens(text) == _estimate_tokens_legacy(text)


def test_estimate_tokens_lone_surrogate_does_not_raise() -> None:
    text = "prefix\ud800suffix"
    assert estimate_tokens(text) == _estimate_tokens_legacy(text)


def test_estimate_tokens_legacy_reference_cases() -> None:
    assert _estimate_tokens_legacy("") == 1
    assert _estimate_tokens_legacy("abcd") == 1
    assert _estimate_tokens_legacy("한글텍스트") == 5
    assert _estimate_tokens_legacy("ab한") == 1