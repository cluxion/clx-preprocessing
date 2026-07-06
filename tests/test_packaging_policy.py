from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import yaml


def test_heavy_local_runtime_is_not_a_package_dependency() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    dependencies = project.get("dependencies", [])
    optional = project.get("optional-dependencies", {})
    all_dependency_specs = list(dependencies)
    for specs in optional.values():
        all_dependency_specs.extend(specs)

    assert not any(spec.lower().startswith("vllm-mlx") for spec in all_dependency_specs)


def test_root_plugin_artifacts_are_version_synced() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    version = pyproject["project"]["version"]

    claude = json.loads(Path(".claude-plugin/plugin.json").read_text(encoding="utf-8"))
    codex = json.loads(Path(".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    root_yaml = yaml.safe_load(Path("plugin.yaml").read_text(encoding="utf-8"))
    package_yaml = yaml.safe_load(Path("src/cluxion_agentplugin_preprocessing/plugin.yaml").read_text(encoding="utf-8"))
    init_source = Path("src/cluxion_agentplugin_preprocessing/__init__.py").read_text(encoding="utf-8")
    fallback = re.search(r'__version__ = "([^"]+)"', init_source)

    assert claude["version"] == version
    assert codex["version"] == version
    assert str(root_yaml["version"]) == version
    assert str(package_yaml["version"]) == version
    assert fallback is not None and fallback.group(1) == version
    assert Path("commands/cluxion-plan.md").is_file()
    assert Path("skills/preprocess/SKILL.md").is_file()


def test_invented_codex_command_snippet_removed() -> None:
    assert not Path("adapters/codex/config-snippet.toml").exists()


def test_marketplace_manifest_is_version_synced() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    version = pyproject["project"]["version"]

    marketplace = json.loads(Path(".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
    assert marketplace["plugins"][0]["version"] == version
    assert marketplace["plugins"][0]["source"] == "./"
