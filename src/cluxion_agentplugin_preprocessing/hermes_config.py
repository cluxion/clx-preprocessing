"""Hermes config helpers for pip-installed entry point plugins."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

PLUGIN_NAME = "cluxion-agentplugin-preprocessing"
LEGACY_PLUGIN_NAME = "hermes-cluxion"


class InvalidConfigError(ValueError):
    """Hermes config is malformed or has an invalid shape."""


@dataclass(frozen=True)
class ConfigChange:
    """Result of a Hermes config update."""

    config_path: Path
    changed: bool
    enabled: bool
    backup_path: Path | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "config_path": str(self.config_path),
            "changed": self.changed,
            "enabled": self.enabled,
            "backup_path": str(self.backup_path) if self.backup_path else "",
        }


def plugin_status(home: str | os.PathLike[str] | None = None) -> ConfigChange:
    """Return whether Hermes config enables this plugin."""
    path = config_path(home)
    config = _read_config(path)
    return ConfigChange(path, False, _is_enabled(config))


def enable_plugin(home: str | os.PathLike[str] | None = None, *, dry_run: bool = False) -> ConfigChange:
    """Add hermes-cluxion to plugins.enabled and remove it from plugins.disabled."""
    path = config_path(home)
    config = _read_config(path)
    changed = _set_enabled(config, enabled=True)
    if dry_run or not changed:
        return ConfigChange(path, changed, True)
    backup = _write_config(path, config)
    return ConfigChange(path, changed, True, backup)


def disable_plugin(home: str | os.PathLike[str] | None = None, *, dry_run: bool = False) -> ConfigChange:
    """Move hermes-cluxion from plugins.enabled to plugins.disabled."""
    path = config_path(home)
    config = _read_config(path)
    changed = _set_enabled(config, enabled=False)
    if dry_run or not changed:
        return ConfigChange(path, changed, False)
    backup = _write_config(path, config)
    return ConfigChange(path, changed, False, backup)


def config_path(home: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the Hermes config path without importing Hermes internals."""
    if home is not None:
        hermes_home = Path(home).expanduser()
    else:
        hermes_home = (
            Path(os.environ.get("HERMES_HOME", "")).expanduser()
            if os.environ.get("HERMES_HOME")
            else Path.home() / ".hermes"
        )
    return hermes_home / "config.yaml"


def _read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        if not raw.strip():
            return {}
        loaded = yaml.safe_load(raw)
    except (UnicodeError, yaml.YAMLError) as exc:
        raise InvalidConfigError(f"invalid YAML in {path}: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise InvalidConfigError(f"{path} must contain a YAML mapping")
    return dict(loaded)


def _is_enabled(config: dict[str, Any]) -> bool:
    plugins = config.get("plugins")
    if not isinstance(plugins, dict):
        return False
    enabled = plugins.get("enabled")
    disabled = plugins.get("disabled")
    return (
        (_contains(enabled, PLUGIN_NAME) or _contains(enabled, LEGACY_PLUGIN_NAME))
        and not _contains(disabled, PLUGIN_NAME)
        and not _contains(disabled, LEGACY_PLUGIN_NAME)
    )


def _set_enabled(config: dict[str, Any], *, enabled: bool) -> bool:
    plugins = config.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise InvalidConfigError("plugins must be a YAML mapping")
    enabled_list = _as_list(plugins.get("enabled"))
    disabled_list = _as_list(plugins.get("disabled"))

    old_enabled = list(enabled_list)
    old_disabled = list(disabled_list)
    if enabled:
        if PLUGIN_NAME not in enabled_list:
            enabled_list.append(PLUGIN_NAME)
        disabled_list = [item for item in disabled_list if item not in {PLUGIN_NAME, LEGACY_PLUGIN_NAME}]
    else:
        enabled_list = [item for item in enabled_list if item not in {PLUGIN_NAME, LEGACY_PLUGIN_NAME}]
        if PLUGIN_NAME not in disabled_list:
            disabled_list.append(PLUGIN_NAME)

    plugins["enabled"] = enabled_list
    if disabled_list:
        plugins["disabled"] = disabled_list
    else:
        plugins.pop("disabled", None)
    return enabled_list != old_enabled or disabled_list != old_disabled


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidConfigError("plugins.enabled/plugins.disabled must be YAML lists")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise InvalidConfigError("plugins.enabled/plugins.disabled entries must be strings")
        items.append(item)
    return items


def _contains(value: object, item: str) -> bool:
    return isinstance(value, list) and item in value


def _write_config(path: Path, config: dict[str, Any]) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = _backup_config(path)
    content = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(content)
        temp_name = handle.name
    Path(temp_name).replace(path)
    return backup


def _backup_config(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup)
    return backup


__all__ = [
    "ConfigChange",
    "InvalidConfigError",
    "config_path",
    "disable_plugin",
    "enable_plugin",
    "plugin_status",
]
