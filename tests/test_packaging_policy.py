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

    # Discovery/public plugin id (Claude/Codex/plugin.yaml). Distro name stays separate.
    assert pyproject["project"]["name"] == "cluxion-agentplugin-preprocessing"
    assert claude["name"] == "clx-preprocessing"
    assert codex["name"] == "clx-preprocessing"
    assert root_yaml["name"] == "clx-preprocessing"
    assert package_yaml["name"] == "clx-preprocessing"
    assert claude["version"] == version
    assert codex["version"] == version
    assert str(root_yaml["version"]) == version
    assert str(package_yaml["version"]) == version
    assert fallback is not None and fallback.group(1) == version
    assert Path("commands/clx-plan.md").is_file()
    assert Path("commands/clx-doctor.md").is_file()
    assert not Path("commands/cluxion-plan.md").exists()
    assert not Path("commands/cluxion-doctor.md").exists()
    skill = Path("skills/clx-preprocess/SKILL.md")
    assert skill.is_file()
    assert yaml.safe_load(skill.read_text(encoding="utf-8").split("---", 2)[1])["name"] == "clx-preprocess"
    assert not Path("skills/preprocess").exists()

    urls = pyproject["project"]["urls"]
    for key in ("Homepage", "Repository", "Issues"):
        assert "clx-preprocessing" in urls[key]
        assert "cluxion-Agentplugin-preprocessing" not in urls[key]
        assert "cluxion-agentplugin-preprocessing" not in urls[key]


def test_invented_codex_command_snippet_removed() -> None:
    assert not Path("adapters/codex/config-snippet.toml").exists()


def test_marketplace_manifest_is_version_synced() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    version = pyproject["project"]["version"]

    marketplace = json.loads(Path(".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
    assert marketplace["name"] == "clx-preprocessing"
    assert marketplace["plugins"][0]["name"] == "clx-preprocessing"
    assert marketplace["plugins"][0]["version"] == version
    assert marketplace["plugins"][0]["source"] == "./"


def test_native_backend_docs_point_at_merged_local_wheel() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    catalog = json.loads(Path("src/cluxion_agentplugin_preprocessing/doctor/catalog.json").read_text(encoding="utf-8"))
    catalog_text = json.dumps(catalog)
    native_entries = [item for item in catalog if item.get("category") == "native_backend"]
    assert native_entries, "expected native_backend catalog entries"

    stale_phrases = (
        "cluxion-queue-native",
        "maturin develop",
        "uv pip install ./rust/cluxion_queue",
        "pip install ./rust/cluxion_queue",
        "separate Rust package",
    )
    for phrase in stale_phrases:
        assert phrase not in readme, f"stale packaging phrase in README: {phrase}"
        assert phrase not in catalog_text, f"stale packaging phrase in catalog: {phrase}"

    assert "bash scripts/build_local_wheel.sh" in readme
    assert 'uv tool install --force "$WHEEL"' in readme
    assert 'HERMES_PY="$HOME/.hermes/hermes-agent/venv/bin/python"' in readme
    assert 'uv pip install --python "$HERMES_PY" --no-deps --reinstall "$WHEEL"' in readme
    assert 'uv pip check --python "$HERMES_PY"' in readme
    assert "shopt" not in readme
    # installation commands must use $WHEEL, not a dist-merged glob
    assert "dist-merged/cluxion_agentplugin_preprocessing-*-*.whl" not in readme
    assert 'uv tool install --force "$WHEEL"' in readme
    assert "WHEEL=" in readme
    assert "exactly one" in readme.lower() or "len(wheels) == 1" in readme or "${#wheels[@]}" in readme

    for entry in native_entries:
        steps = "\n".join(entry.get("fix_steps", []))
        for phrase in stale_phrases:
            assert phrase not in steps, f"{entry['check_id']} still has {phrase!r}"
        if entry["check_id"] in {"native_module_importable", "abi3_wheel_compatible"}:
            assert "scripts/build_local_wheel.sh" in steps
            assert "cluxion_queue_native" in steps or "import cluxion_queue_native" in steps
            assert 'WHEEL="$(python3 -c' in steps
            assert "assert len(w)==1" in steps

    assert Path("scripts/build_local_wheel.sh").is_file()
