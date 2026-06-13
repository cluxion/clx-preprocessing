"""Deterministic intent and direction classification for agent work."""

from __future__ import annotations

from cluxion_runtime.core.types import AgentSurface, WorkIntent, WorkItem


def classify_intent(item: WorkItem) -> WorkIntent:
    """Classify user intent before the host model spends context."""
    text = f"{item.prompt}\n{item.model_route}".lower()
    signals: list[str] = []
    local = _has_any(text, ("vllm-mlx", "mlx", "local model", "로컬모델", "로컬 모델", "serve-local"))
    if item.model_route.lower().startswith(("local/", "mlx/", "vllm-mlx/", "vllm_mlx/")):
        local = True
        signals.append("explicit_local_route")
    if local:
        signals.append("local_model")
        return WorkIntent("local_model", "serve_endpoint", True, "local_runtime", 0.92, tuple(signals))

    if _has_any(text, ("security", "audit", "vulnerability", "보안", "취약점", "시크릿")):
        signals.append("security")
        return WorkIntent("security", "review_risk", False, "host_managed", 0.86, tuple(signals))

    if _has_any(text, ("test", "pytest", "unit", "테스트", "검증")):
        signals.append("test")
        return WorkIntent(
            "engineering", "verify_or_fix", False, _direction_for_surface(item.surface), 0.82, tuple(signals)
        )

    if _has_any(text, ("code", "implement", "fix", "refactor", "패치", "수정", "구현", "리팩터")):
        signals.append("coding")
        return WorkIntent(
            "engineering", "code_change", False, _direction_for_surface(item.surface), 0.80, tuple(signals)
        )

    if _has_any(text, ("docs", "readme", "문서", "가이드", "사용법")):
        signals.append("documentation")
        return WorkIntent("documentation", "write_or_update_docs", False, "host_managed", 0.78, tuple(signals))

    return WorkIntent("general", "plan_task", False, _direction_for_surface(item.surface), 0.55, tuple(signals))


def _direction_for_surface(surface: AgentSurface) -> str:
    if surface == AgentSurface.HERMES:
        return "hermes_harness"
    if surface == AgentSurface.CODEX:
        return "codex_harness"
    if surface == AgentSurface.CLAUDE:
        return "claude_harness"
    if surface == AgentSurface.GROK_BUILD:
        return "grok_build_harness"
    return "host_managed"


def _has_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


__all__ = ["classify_intent"]
