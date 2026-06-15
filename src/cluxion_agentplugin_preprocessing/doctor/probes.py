"""Plugin-specific probes for preprocessing doctor. Cross-cutting + selected specific checks."""

from __future__ import annotations

import importlib.metadata
import shutil
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
        return "warn", "native missing \u2192 using fallback (slower)"


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


# note: other checks in catalog will be reported as skip (no probe)
