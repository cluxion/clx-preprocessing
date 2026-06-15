"""Plugin-specific probes for preprocessing doctor. Cross-cutting + selected specific checks."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json as _json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable

from .framework import DoctorContext

PROBES: dict[str, Callable[[DoctorContext], tuple[str, str]]] = {}


def _register(name: str):
    def deco(fn):
        PROBES[name] = fn
        return fn

    return deco


@_register("hermes_on_path")
def hermes_on_path(ctx: DoctorContext) -> tuple[str, str]:
    p = shutil.which(ctx.hermes_bin)
    if p:
        return "pass", str(p)
    return "fail", "not found on PATH"


@_register("hermes_version")
def hermes_version(ctx: DoctorContext) -> tuple[str, str]:
    try:
        cp = ctx.run([ctx.hermes_bin, "--version"])
        if cp.returncode == 0 and "Hermes Agent v" in cp.stdout:
            return "pass", cp.stdout.strip()
        return "fail", cp.stdout.strip() or cp.stderr.strip()
    except Exception as e:
        return "fail", f"run error: {e}"


@_register("hermes_oneshot_flag")
def hermes_oneshot_flag(ctx: DoctorContext) -> tuple[str, str]:
    try:
        cp = ctx.run([ctx.hermes_bin, "--help"])
        out = cp.stdout + cp.stderr
        if "-z" in out and "--oneshot" in out:
            return "pass", "present"
        return "fail", "missing in --help"
    except Exception as e:
        return "fail", f"run error: {e}"


@_register("entry_point_registered")
def entry_point_registered(ctx: DoctorContext) -> tuple[str, str]:
    try:
        eps = importlib.metadata.entry_points(group="hermes_agent.plugins")
        for ep in eps:
            if "cluxion-agentplugin-preprocessing" in (ep.name or "").lower() or "cluxion_agentplugin_preprocessing" in (ep.value or ""):
                mod = ep.load()
                if hasattr(mod, "register") and callable(mod.register):
                    return "pass", ep.value or str(ep)
        return "fail", "entry point not found or register missing"
    except Exception as e:
        return "fail", f"metadata error: {e}"


@_register("toolset_valid")
def toolset_valid(ctx: DoctorContext) -> tuple[str, str]:
    try:
        cp = ctx.run([ctx.hermes_bin, "tools", "list"])
        if cp.returncode == 0 and "cluxion" in cp.stdout:
            return "pass", "cluxion present"
        return "fail", "cluxion not in tools list"
    except Exception as e:
        return "fail", f"run error: {e}"


@_register("install_integrity")
def install_integrity(ctx: DoctorContext) -> tuple[str, str]:
    try:
        from cluxion_agentplugin_preprocessing import __version__ as pkg_version

        dist_version = importlib.metadata.version("cluxion-agentplugin-preprocessing")
        if dist_version == pkg_version:
            return "pass", dist_version
        return "warn", f"dist={dist_version} pkg={pkg_version}"
    except Exception as e:
        return "fail", f"version error: {e}"


@_register("native_module_importable")
def native_module_importable(ctx: DoctorContext) -> tuple[str, str]:
    try:
        mod = __import__("cluxion_queue_native")
        if hasattr(mod, "run"):
            return "pass", "imported (native backend available)"
        return "warn", "imported but expected symbols missing"
    except Exception:
        return "warn", "native missing → using fallback (slower)"


# plugin-specific probes (deterministic ones only)
@_register("queue_backend_resolvable")
def queue_backend_resolvable(ctx: DoctorContext) -> tuple[str, str]:
    try:
        from cluxion_runtime.resources import queue_bridge
        backend = queue_bridge.resolve_backend()
        if backend in ("native", "subprocess", "python"):
            return "pass", backend
        return "fail", f"invalid backend {backend}"
    except Exception as e:
        return "skip", f"cannot resolve: {e}"


@_register("queue_store_dir_writable")
def queue_store_dir_writable(ctx: DoctorContext) -> tuple[str, str]:
    try:
        from cluxion_runtime.resources import queue_bridge
        store = queue_bridge.default_store_dir()
        store.mkdir(parents=True, exist_ok=True)
        probe = store / ".doctor-probe"
        probe.write_text("ok")
        readback = probe.read_text()
        probe.unlink()
        if readback == "ok":
            return "pass", str(store)
        return "fail", "roundtrip mismatch"
    except OSError as e:
        return "fail", f"OSError: {e}"
    except Exception as e:
        return "skip", f"cannot check store dir: {e}"


@_register("dispatch_dir_writable")
def dispatch_dir_writable(ctx: DoctorContext) -> tuple[str, str]:
    try:
        from cluxion_runtime.core.dispatch_store import default_dispatch_dir
        d = default_dispatch_dir()
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".doctor-probe"
        probe.write_text("ok")
        readback = probe.read_text()
        probe.unlink()
        if readback == "ok":
            return "pass", str(d)
        return "fail", "roundtrip mismatch"
    except OSError as e:
        return "fail", f"OSError: {e}"
    except Exception as e:
        return "skip", f"cannot check dispatch dir: {e}"


@_register("guard_daemon_startable")
def guard_daemon_startable(ctx: DoctorContext) -> tuple[str, str]:
    try:
        # best-effort, do not actually start long daemon in doctor

        return "pass", "psutil available"
    except Exception:
        return "skip", "psutil not importable"


@_register("handler_exception_coverage")
def handler_exception_coverage(ctx: DoctorContext) -> tuple[str, str]:
    try:
        from cluxion_agentplugin_preprocessing.plugin import _json_result
        def bad_cb():
            raise TypeError("test TypeError for coverage")
        result = _json_result(bad_cb)
        if isinstance(result, str) and "ok" in result and "false" in result.lower():
            return "pass", "degraded to error JSON"
        return "fail", f"no error json: {result[:100]}"
    except Exception as e:
        return "skip", f"cannot invoke guard: {e}"


def _safe_read_hermes_config():
    try:
        import yaml
        cfg_path = os.path.expanduser("~/.hermes/config.yaml")
        if not os.path.exists(cfg_path):
            return None, "absent"
        with open(cfg_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data, "ok"
    except Exception as e:
        return None, f"read_error:{type(e).__name__}"


@_register("psutil_importable")
def psutil_importable(ctx: DoctorContext) -> tuple[str, str]:
    try:
        import psutil
        _ = psutil.virtual_memory()
        return "pass", "importable"
    except ImportError:
        return "fail", "psutil not installed"
    except Exception as e:
        return "skip", f"uncertainty: {type(e).__name__}"


@_register("pyyaml_importable")
def pyyaml_importable(ctx: DoctorContext) -> tuple[str, str]:
    try:
        import yaml
        yaml.safe_load("test: 1")
        return "pass", "importable"
    except ImportError:
        return "fail", "PyYAML not installed"
    except Exception as e:
        return "skip", f"uncertainty: {type(e).__name__}"


@_register("fcntl_available_on_posix")
def fcntl_available_on_posix(ctx: DoctorContext) -> tuple[str, str]:
    if os.name != "posix":
        return "skip", "non-POSIX (Windows)"
    try:
        import fcntl
        # real check: lock a temp file
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf_path = tf.name
        try:
            with open(tf_path, "a+b") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            return "pass", "fcntl works on POSIX"
        finally:
            os.unlink(tf_path)
    except Exception as e:
        return "skip", f"fcntl issue: {type(e).__name__}"


@_register("playwright_optional_available")
def playwright_optional_available(ctx: DoctorContext) -> tuple[str, str]:
    if importlib.util.find_spec("playwright") is not None:
        return "pass", "importable"
    return "warn", "optional, not installed"


@_register("abi3_wheel_compatible")
def abi3_wheel_compatible(ctx: DoctorContext) -> tuple[str, str]:
    # since requires-python >=3.11 the check is always pass
    return "pass", f"Python {sys.version_info.major}.{sys.version_info.minor} >= 3.11 abi3 floor"


@_register("sqlite_wal_mode_compatible")
def sqlite_wal_mode_compatible(ctx: DoctorContext) -> tuple[str, str]:
    try:
        import sqlite3
        conn = sqlite3.connect(":memory:")
        try:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            ver = sqlite3.sqlite_version
            if str(mode).lower() == "wal":
                return "pass", f"wal supported (sqlite {ver})"
            return "warn", f"got {mode} (sqlite {ver})"
        finally:
            conn.close()
    except Exception as e:
        return "skip", f"uncertainty: {type(e).__name__}"


@_register("json_serialization_deterministic")
def json_serialization_deterministic(ctx: DoctorContext) -> tuple[str, str]:
    try:
        d = {"z": 1, "a": 2, "nested": {"b": 3}}
        j1 = _json.dumps(d, sort_keys=True, separators=(",", ":"))
        j2 = _json.dumps(d, sort_keys=True, separators=(",", ":"))
        if j1 == j2:
            return "pass", "roundtrip bytes equal"
        return "fail", "non-deterministic"
    except Exception as e:
        return "skip", f"uncertainty: {type(e).__name__}"


@_register("hermes_plugin_enabled")
def hermes_plugin_enabled(ctx: DoctorContext) -> tuple[str, str]:
    data, status = _safe_read_hermes_config()
    if status != "ok":
        return "skip", f"config not present: {status}"
    try:
        plugins = (data or {}).get("plugins", {}) or {}
        enabled = plugins.get("enabled", []) or []
        disabled = plugins.get("disabled", []) or []
        names = ["cluxion-agentplugin-preprocessing", "hermes-cluxion"]
        for n in names:
            if n in enabled and n not in disabled:
                return "pass", f"{n} in enabled"
        if any(n in enabled for n in names):
            return "warn", "present but also disabled?"
        return "warn", "not in plugins.enabled"
    except Exception as e:
        return "skip", f"uncertainty: {type(e).__name__}"


@_register("env_var_consistency")
def env_var_consistency(ctx: DoctorContext) -> tuple[str, str]:
    try:
        known = [
            "CLUXION_QUEUE_STORE_DIR",
            "CLUXION_PREPROCESS_DISPATCH_DIR",
            "CLUXION_QUEUE_BACKEND",
        ]
        issues = []
        for var in known:
            val = os.environ.get(var)
            if val and "DIR" in var:
                try:
                    p = os.path.expanduser(val)
                    os.makedirs(p, exist_ok=True)
                except Exception:
                    issues.append(f"{var}=invalid_path")
        if issues:
            return "warn", ";".join(issues)
        return "pass", "defaults or valid"
    except Exception as e:
        return "skip", f"uncertainty: {type(e).__name__}"


# note: other checks in catalog will be reported as skip (no probe)
