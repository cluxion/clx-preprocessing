from __future__ import annotations

import tomllib
from pathlib import Path


def test_heavy_local_runtime_is_not_a_package_dependency() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    dependencies = project.get("dependencies", [])
    optional = project.get("optional-dependencies", {})
    all_dependency_specs = list(dependencies)
    for specs in optional.values():
        all_dependency_specs.extend(specs)

    assert not any(spec.lower().startswith("vllm-mlx") for spec in all_dependency_specs)
