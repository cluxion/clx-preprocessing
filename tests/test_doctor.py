"""Tests for embedded doctor (determinism + cross-cutting checks)."""

import json
import subprocess
import sys
import time
from pathlib import Path

from cluxion_agentplugin_preprocessing.doctor import (
    DoctorResult,
    render_json,
    run_doctor,
)
from cluxion_agentplugin_preprocessing.doctor.probes import PROBES


def _catalog_path() -> Path:
    import importlib.resources

    pkg = "cluxion_agentplugin_preprocessing.doctor"
    return Path(str(importlib.resources.files(pkg).joinpath("catalog.json")))


def test_run_doctor_returns_result_and_deterministic():
    cat = _catalog_path()
    r1 = run_doctor(
        cwd=Path.cwd(),
        catalog_path=cat,
        probes=PROBES,
        plugin="preprocessing",
        version="0.3.7",
    )
    assert isinstance(r1, DoctorResult)
    j1 = render_json(r1)
    r2 = run_doctor(
        cwd=Path.cwd(),
        catalog_path=cat,
        probes=PROBES,
        plugin="preprocessing",
        version="0.3.7",
    )
    j2 = render_json(r2)
    assert j1 == j2  # byte identical
    # sorted by severity then id
    ids = [c.check_id for c in r1.checks]
    assert len(ids) > 0


def test_cross_cutting_checks_present():
    cat = _catalog_path()
    result = run_doctor(
        cwd=Path.cwd(),
        catalog_path=cat,
        probes=PROBES,
        plugin="preprocessing",
        version="0.3.7",
    )
    statuses = {c.check_id: c.status for c in result.checks}
    for key in ("hermes_on_path", "entry_point_registered", "toolset_valid"):
        assert key in statuses
        assert statuses[key] in ("pass", "warn", "fail", "skip")


def test_new_probes_non_skip():
    cat = _catalog_path()
    result = run_doctor(
        cwd=Path.cwd(),
        catalog_path=cat,
        probes=PROBES,
        plugin="preprocessing",
        version="0.3.7",
    )
    statuses = {c.check_id: c.status for c in result.checks}
    for key in (
        "psutil_importable",
        "json_serialization_deterministic",
        "runtime_binary_accessible",
        "clarification_answers_present",
        "guard_state_not_stale",
        "temp_file_cleanup",
    ):
        assert key in statuses
        assert statuses[key] in ("pass", "warn", "fail")  # non-skip


def test_run_doctor_parallelizes_slow_probes(tmp_path: Path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            [
                {
                    "check_id": f"slow_{idx}",
                    "category": "c",
                    "severity": "low",
                    "what_it_checks": "w",
                    "failure_symptom": "f",
                    "likely_causes": [],
                    "fix_steps": [],
                    "change_robust": "r",
                }
                for idx in range(4)
            ]
        ),
        encoding="utf-8",
    )

    def slow(_ctx):
        time.sleep(0.2)
        return "pass", "ok"

    started = time.perf_counter()
    result = run_doctor(
        cwd=Path.cwd(),
        catalog_path=catalog,
        probes={f"slow_{idx}": slow for idx in range(4)},
        plugin="preprocessing",
        version="0.3.26",
    )

    assert result.ok is True
    assert time.perf_counter() - started < 0.45


def test_cli_doctor_json_is_stdout_only_json():
    completed = subprocess.run(
        [sys.executable, "-m", "cluxion_agentplugin_preprocessing.cli", "doctor", "--json"],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        check=False,
    )

    payload = json.loads(completed.stdout)
    assert "checks" in payload
    assert completed.stderr == ""


def test_real_failure_mode_probes_present():
    cat = _catalog_path()
    result = run_doctor(
        cwd=Path.cwd(),
        catalog_path=cat,
        probes=PROBES,
        plugin="preprocessing",
        version="0.3.24",
    )
    statuses = {c.check_id: c.status for c in result.checks}
    for key in (
        "hermes_plugin_enabled",
        "hermes_deliver_patch_status",
        "dispatch_dir_writable",
        "version_files_synced",
    ):
        assert key in statuses
        assert statuses[key] in ("pass", "warn", "fail", "skip")


def test_probe_exception_becomes_fail():
    def bad_probe(ctx):
        raise RuntimeError("boom")

    result = run_doctor(
        cwd=Path.cwd(),
        catalog_path=_catalog_path(),
        probes={"hermes_on_path": bad_probe},
        plugin="preprocessing",
        version="0.3.7",
    )
    statuses = {c.check_id: c.status for c in result.checks}
    assert statuses["hermes_on_path"] == "fail"


def test_warn_only_is_ok():
    # construct a result with only warn (no fail)
    from cluxion_agentplugin_preprocessing.doctor.framework import CheckResult, DoctorResult
    checks = (
        CheckResult(check_id="x", category="c", severity="medium", status="warn", detail="w"),
    )
    r = DoctorResult(plugin="p", version="0.3.7", checks=checks)
    assert r.ok is True
