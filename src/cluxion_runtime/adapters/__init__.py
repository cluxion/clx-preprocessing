"""Thin adapter contracts for Hermes, Codex, and Claude."""

from __future__ import annotations

from cluxion_runtime.adapters.contract import render_adapter_manifest, work_item_from_adapter_payload
from cluxion_runtime.adapters.hermes import (
    HermesLocalEndpointPatch,
    build_hermes_local_endpoint_patch,
    hermes_config_patch_to_dict,
    hermes_config_set_commands,
    render_hermes_yaml_fragment,
)
from cluxion_runtime.adapters.spec import AgentAdapterSpec, default_adapter_specs

__all__ = [
    "AgentAdapterSpec",
    "HermesLocalEndpointPatch",
    "build_hermes_local_endpoint_patch",
    "default_adapter_specs",
    "hermes_config_patch_to_dict",
    "hermes_config_set_commands",
    "render_adapter_manifest",
    "render_hermes_yaml_fragment",
    "work_item_from_adapter_payload",
]
