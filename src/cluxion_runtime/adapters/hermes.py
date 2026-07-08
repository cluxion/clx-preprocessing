"""Generator for Hermes Agent local endpoint config patches."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class HermesLocalEndpointPatch:
    """Patch making Hermes see the Cluxion local server as an official custom provider."""

    provider_key: str
    display_name: str
    model: str
    base_url: str
    context_length: int = 128_000
    transport: str = "openai_chat"

    @property
    def provider_id(self) -> str:
        """Provider ID for Hermes model.provider."""
        return f"custom:{self.provider_key}"

    @property
    def slash_model(self) -> str:
        """/model value for switching directly inside a Hermes session."""
        return f"{self.provider_id}:{self.model}"


def build_hermes_local_endpoint_patch(
    model: str,
    base_url: str,
    *,
    provider_key: str = "cluxion-local",
    display_name: str = "Cluxion Local",
    context_length: int = 128_000,
) -> HermesLocalEndpointPatch:
    """Validate inputs and build a Hermes config patch object."""
    clean_model = model.strip()
    clean_url = base_url.strip().rstrip("/")
    clean_key = _provider_key(provider_key)
    if not clean_model:
        raise ValueError("Hermes local endpoint model must not be empty.")
    if not _valid_http_url(clean_url):
        raise ValueError("Hermes local endpoint base_url must be an http(s) URL.")
    if context_length <= 0:
        raise ValueError("Hermes context_length must be positive.")
    return HermesLocalEndpointPatch(
        provider_key=clean_key,
        display_name=display_name.strip() or "Cluxion Local",
        model=clean_model,
        base_url=clean_url,
        context_length=context_length,
    )


def hermes_config_patch_to_dict(patch: HermesLocalEndpointPatch) -> dict[str, object]:
    """Build a dict patch mergeable into Hermes config.yaml."""
    provider_entry = {
        "name": patch.display_name,
        "base_url": patch.base_url,
        "default_model": patch.model,
        "transport": patch.transport,
        "discover_models": True,
        "models": {
            patch.model: {
                "context_length": patch.context_length,
            }
        },
    }
    return {
        "providers": {
            patch.provider_key: provider_entry,
        },
        "model": {
            "provider": patch.provider_id,
            "default": patch.model,
            "base_url": patch.base_url,
            "context_length": patch.context_length,
            "api_mode": "chat_completions",
        },
    }


def render_hermes_yaml_fragment(patch: HermesLocalEndpointPatch) -> str:
    """Build a YAML fragment the user can paste into config.yaml."""
    model = _yaml_str(patch.model)
    base_url = _yaml_str(patch.base_url)
    name = _yaml_str(patch.display_name)
    provider_id = _yaml_str(patch.provider_id)
    return "\n".join(
        [
            "providers:",
            f"  {patch.provider_key}:",
            f"    name: {name}",
            f"    base_url: {base_url}",
            f"    default_model: {model}",
            f"    transport: {patch.transport}",
            "    discover_models: true",
            "    models:",
            f"      {model}:",
            f"        context_length: {patch.context_length}",
            "model:",
            f"  provider: {provider_id}",
            f"  default: {model}",
            f"  base_url: {base_url}",
            f"  context_length: {patch.context_length}",
            "  api_mode: chat_completions",
        ]
    )


def hermes_config_set_commands(patch: HermesLocalEndpointPatch) -> tuple[str, ...]:
    """Commands reaching the required provider state via Hermes config set."""
    root = f"providers.{patch.provider_key}"
    return (
        f"hermes config set {root}.name {_shell_quote(patch.display_name)}",
        f"hermes config set {root}.base_url {_shell_quote(patch.base_url)}",
        f"hermes config set {root}.default_model {_shell_quote(patch.model)}",
        f"hermes config set {root}.transport {patch.transport}",
        f"hermes config set {root}.discover_models true",
        f"hermes config set {root}.models.{patch.model}.context_length {patch.context_length}",
        f"hermes config set model.provider {_shell_quote(patch.provider_id)}",
        f"hermes config set model.default {_shell_quote(patch.model)}",
        f"hermes config set model.base_url {_shell_quote(patch.base_url)}",
        f"hermes config set model.context_length {patch.context_length}",
        "hermes config set model.api_mode chat_completions",
    )


def _provider_key(value: str) -> str:
    clean = value.strip().lower().replace("_", "-").replace(" ", "-")
    allowed = [ch for ch in clean if ch.isalnum() or ch == "-"]
    compact = "".join(allowed).strip("-")
    if not compact:
        raise ValueError("Hermes provider key must not be empty.")
    return compact


def _valid_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _yaml_str(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _shell_quote(value: str) -> str:
    escaped = value.replace("'", "'\"'\"'")
    return f"'{escaped}'"


__all__ = [
    "HermesLocalEndpointPatch",
    "build_hermes_local_endpoint_patch",
    "hermes_config_patch_to_dict",
    "hermes_config_set_commands",
    "render_hermes_yaml_fragment",
]
