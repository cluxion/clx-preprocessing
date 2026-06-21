"""Small data contracts for the Cluxion runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum


class AgentSurface(StrEnum):
    """Agent surface sitting in front of Cluxion."""

    HERMES = "hermes"
    CODEX = "codex"
    CLAUDE = "claude"
    GROK_BUILD = "grok_build"
    LOCAL = "local"
    API = "api"


class RuntimeKind(StrEnum):
    """Actual model execution backend."""

    HOST_MANAGED = "host_managed"
    VLLM_MLX = "vllm_mlx"
    MLX_LM = "mlx_lm"
    OLLAMA = "ollama"
    OPENAI_COMPAT = "openai_compat"
    GENERIC = "generic"


class WorkPriority(IntEnum):
    """Work queue priority. Lower numbers run first."""

    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3


@dataclass(frozen=True)
class WorkItem:
    """A single work item handed to Cluxion by an external agent."""

    work_id: str
    prompt: str
    surface: AgentSurface = AgentSurface.API
    priority: WorkPriority = WorkPriority.NORMAL
    model_route: str = "host/default"
    expected_ram_mb: int = 0
    context_tokens: int = 0
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class QueueSegment:
    """Segment of a long task split into an executable unit."""

    segment_id: str
    char_start: int
    char_end: int
    token_estimate: int
    checksum: str
    preview: str
    content: str


@dataclass(frozen=True)
class AnswerPolicy:
    """Answer-safety contract the host agent must honour before calling the model."""

    unknown_behavior: str = "say_unknown_if_insufficient_context"
    source_policy: str = "do_not_invent_sources_or_facts"
    scope: str = "answer_only_from_available_context_and_verified_runtime_state"
    response_contract: str = "direct_answer_with_uncertainty_boundary"
    verification_required: bool = False
    citation_required: bool = False
    uncertainty_level: str = "low"
    required_checks: tuple[str, ...] = ()
    grounding: tuple[str, ...] = (
        "verified_facts",
        "explicit_user_context",
        "tool_results",
        "clearly_labeled_inferences",
    )
    rules: tuple[str, ...] = (
        "If the available context is insufficient, say that clearly before proceeding.",
        "Do not fabricate file state, external facts, tool results, or model availability.",
        "For current or environment-specific claims, verify through tools before presenting them as facts.",
        "Separate verified facts from inferences and unknowns when accuracy matters.",
        "If a check was not run, say it was not run; do not imply that it passed.",
    )


@dataclass(frozen=True)
class PreprocessResult:
    """Deterministic preprocessing result computed before any model call."""

    normalized_prompt: str
    segments: tuple[QueueSegment, ...]
    token_estimate: int
    split_required: bool
    effort: str
    evidence: tuple[str, ...]
    mode: str = "standard"
    preprocess_required: bool = True
    reason_codes: tuple[str, ...] = ()
    answer_policy: AnswerPolicy = field(default_factory=AnswerPolicy)


@dataclass(frozen=True)
class WorkIntent:
    """Deterministic user intent and routing direction."""

    category: str
    operation: str
    local_model_requested: bool
    direction: str
    confidence: float
    signals: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResourceSnapshot:
    """Resource snapshot from Rust or the Python fallback."""

    total_ram_mb: int
    available_ram_mb: int
    swap_used_mb: int
    cpu_percent: float
    zombie_pids: tuple[int, ...] = ()


@dataclass(frozen=True)
class ResourceDecision:
    """Execution admission verdict."""

    allowed: bool
    mode: str
    reason: str
    recommended_parallel: int
    work_kind: str
    dispatch_memory_budget_mb: int
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelRuntimeProfile:
    """Runtime profile for a local model server."""

    kind: RuntimeKind
    model: str
    base_url: str
    command: tuple[str, ...]
    health_path: str = "/v1/models"


@dataclass(frozen=True)
class HostExecutionStep:
    """Host-owned execution step for the current Hermes model."""

    step_id: str
    kind: str
    prompt: str
    segment_id: str = ""
    checksum: str = ""
    token_estimate: int = 0
    depends_on: tuple[str, ...] = ()
    required_checks: tuple[str, ...] = ()


@dataclass(frozen=True)
class HostExecutionPlan:
    """Contract where Cluxion only specifies host-model usage and never calls a model itself."""

    model_owner: str
    provider_policy: str
    strategy: str
    queue_required: bool
    synthesis_required: bool
    preflight_required: bool
    max_extra_model_calls: int
    steps: tuple[HostExecutionStep, ...]
    next_tool: str = ""
    record_tool: str = ""
    brief_tool: str = ""
    loop_tool: str = ""
    performance_notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class HarnessPlan:
    """Execution plan for work handed to Cluxion by an agent surface."""

    item: WorkItem
    intent: WorkIntent
    preprocessing: PreprocessResult
    resource: ResourceDecision
    runtime: ModelRuntimeProfile
    execution: HostExecutionPlan
    queue_position: int = 0
    clarification_required: bool = False
    clarification_questions: tuple[str, ...] = ()
    queue_backend: str = "python"


__all__ = [
    "AgentSurface",
    "AnswerPolicy",
    "HarnessPlan",
    "HostExecutionPlan",
    "HostExecutionStep",
    "ModelRuntimeProfile",
    "PreprocessResult",
    "QueueSegment",
    "ResourceDecision",
    "ResourceSnapshot",
    "RuntimeKind",
    "WorkIntent",
    "WorkItem",
    "WorkPriority",
]
