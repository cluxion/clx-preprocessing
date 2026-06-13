"""Thin per-agent install contract."""

from __future__ import annotations

from dataclasses import dataclass

from cluxion_runtime.core.types import AgentSurface


@dataclass(frozen=True)
class AgentAdapterSpec:
    """Minimum information each platform wrapper must know."""

    surface: AgentSurface
    config_target: str
    transport: str
    install_hint: str


def default_adapter_specs() -> tuple[AgentAdapterSpec, ...]:
    """Official adapters attached to the shared runtime."""
    return (
        AgentAdapterSpec(
            AgentSurface.HERMES, "~/.hermes/plugins/cluxion", "local_override", "Hermes local model helper"
        ),
        AgentAdapterSpec(AgentSurface.CODEX, "~/.codex/config.toml", "local_override", "Codex local endpoint helper"),
        AgentAdapterSpec(
            AgentSurface.CLAUDE, ".claude-plugin/plugin.json", "local_override", "Claude Code local endpoint helper"
        ),
        AgentAdapterSpec(
            AgentSurface.GROK_BUILD,
            "project agent config",
            "local_override",
            "Grok Build local endpoint helper",
        ),
    )


__all__ = ["AgentAdapterSpec", "default_adapter_specs"]
