from __future__ import annotations

import json
from typing import TYPE_CHECKING

import yaml

from cluxion_agentplugin_preprocessing import cli, hermes_config
from cluxion_runtime.adapters.hermes import (
    build_hermes_local_endpoint_patch,
    hermes_config_patch_to_dict,
    hermes_config_set_commands,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_enable_plugin_creates_config(tmp_path: Path) -> None:
    result = hermes_config.enable_plugin(tmp_path)
    data = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))

    assert result.changed is True
    assert result.enabled is True
    assert result.backup_path is None
    assert data["plugins"]["enabled"] == ["cluxion-agentplugin-preprocessing"]


def test_enable_plugin_removes_disabled_and_backs_up_existing_config(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "model:\n  provider: test\nplugins:\n  disabled:\n    - hermes-cluxion\n    - other\n",
        encoding="utf-8",
    )

    result = hermes_config.enable_plugin(tmp_path)
    data = yaml.safe_load(config.read_text(encoding="utf-8"))

    assert result.changed is True
    assert result.backup_path is not None
    assert result.backup_path.exists()
    assert data["model"]["provider"] == "test"
    assert data["plugins"]["enabled"] == ["cluxion-agentplugin-preprocessing"]
    assert data["plugins"]["disabled"] == ["other"]


def test_enable_plugin_is_idempotent(tmp_path: Path) -> None:
    hermes_config.enable_plugin(tmp_path)
    result = hermes_config.enable_plugin(tmp_path)

    assert result.changed is False
    assert result.backup_path is None


def test_disable_plugin_moves_to_disabled(tmp_path: Path) -> None:
    hermes_config.enable_plugin(tmp_path)
    result = hermes_config.disable_plugin(tmp_path)
    data = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))

    assert result.changed is True
    assert result.enabled is False
    assert data["plugins"]["enabled"] == []
    assert data["plugins"]["disabled"] == ["cluxion-agentplugin-preprocessing"]


def test_cli_enable_reports_json(tmp_path: Path, capsys) -> None:
    code = cli.main(["enable", "--home", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["ok"] is True
    assert payload["enabled"] is True
    assert payload["changed"] is True


def test_hermes_config_set_commands_include_provider_model_context_length() -> None:
    patch = build_hermes_local_endpoint_patch(
        "local-128k",
        "http://127.0.0.1:8787/v1",
        context_length=200000,
    )

    assert (
        "hermes config set providers.cluxion-local.models.local-128k.context_length 200000"
        in hermes_config_set_commands(patch)
    )
    assert (
        hermes_config_patch_to_dict(patch)["providers"]["cluxion-local"]["models"][
            "local-128k"
        ]["context_length"]
        == 200000
    )
