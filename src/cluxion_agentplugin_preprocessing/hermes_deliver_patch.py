"""Apply and verify Hermes core support for plugin slash ``deliver=agent``."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Literal

PATCH_RESOURCE = "patches/hermes-deliver-agent.patch"
BRANCH_NAME = "cluxion/plugin-deliver-agent"
_GIT_TIMEOUT_SECONDS = 60.0

Status = Literal["applied", "missing", "partial", "no_hermes", "anchors-mismatch", "timeout"]


@dataclass(frozen=True)
class PatchResult:
    hermes_root: Path
    status: Status
    applied: bool
    changed: bool
    method: str
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "hermes_root": str(self.hermes_root),
            "status": self.status,
            "applied": self.applied,
            "changed": self.changed,
            "method": self.method,
            "detail": self.detail,
        }


def resolve_hermes_agent_root(home: str | os.PathLike[str] | None = None) -> Path | None:
    """Best-effort Hermes agent source tree (contains ``hermes_cli/``)."""
    candidates: list[Path] = []
    env_root = os.environ.get("HERMES_AGENT_ROOT", "").strip()
    if env_root:
        candidates.append(Path(env_root).expanduser())
    if home is not None:
        candidates.append(Path(home).expanduser() / "hermes-agent")
    hermes_home = (
        Path(os.environ.get("HERMES_HOME", "")).expanduser()
        if os.environ.get("HERMES_HOME")
        else Path.home() / ".hermes"
    )
    candidates.append(hermes_home / "hermes-agent")

    hermes_bin = shutil.which("hermes")
    if hermes_bin:
        try:
            resolved = Path(hermes_bin).resolve()
            for parent in resolved.parents:
                if (parent / "hermes_cli" / "plugins.py").is_file():
                    candidates.append(parent)
                    break
        except OSError:
            pass

    seen: set[Path] = set()
    for root in candidates:
        if root in seen:
            continue
        seen.add(root)
        if (root / "hermes_cli" / "plugins.py").is_file():
            return root
    return None


def patch_status(hermes_root: Path | None = None) -> PatchResult:
    root = hermes_root or resolve_hermes_agent_root()
    if root is None:
        return PatchResult(Path(), "no_hermes", False, False, "none", "hermes-agent tree not found")

    checks = {
        "plugins": _plugins_applied(root),
        "cli": _cli_applied(root),
        "gateway_dispatch": _gateway_dispatch_applied(root),
        "gateway_slash_exec": _gateway_slash_exec_applied(root),
    }
    ok_count = sum(1 for v in checks.values() if v)
    if ok_count == len(checks):
        return PatchResult(root, "applied", True, False, "verify", "all markers present")
    if ok_count == 0:
        return PatchResult(root, "missing", False, False, "verify", f"missing: {checks}")
    return PatchResult(root, "partial", False, False, "verify", f"partial: {checks}")


def ensure_applied(
    *,
    hermes_root: Path | None = None,
    dry_run: bool = False,
) -> PatchResult:
    """Idempotently apply deliver=agent support to the local Hermes checkout."""
    current = patch_status(hermes_root)
    if current.status == "no_hermes":
        return current
    if current.status == "applied":
        return PatchResult(current.hermes_root, "applied", True, False, "noop", "already applied")

    if dry_run:
        return PatchResult(
            current.hermes_root,
            current.status,
            False,
            False,
            "dry_run",
            f"would apply ({current.detail})",
        )

    try:
        for method, apply_method in (
            ("git_branch", _apply_via_git_branch),
            ("git_apply", _apply_via_git_patch),
            ("inline", _apply_inline),
        ):
            result = apply_method(current.hermes_root)
            if result:
                after = patch_status(current.hermes_root)
                if after.status == "applied":
                    return PatchResult(
                        current.hermes_root,
                        "applied",
                        True,
                        True,
                        method,
                        "patch applied successfully",
                    )
    except subprocess.TimeoutExpired as exc:
        return PatchResult(current.hermes_root, "timeout", False, False, "timeout", _timeout_detail(exc))

    after = patch_status(current.hermes_root)
    if after.status != "applied":
        return PatchResult(
            current.hermes_root,
            "anchors-mismatch",
            False,
            False,
            "failed",
            after.detail,
        )
    return PatchResult(
        current.hermes_root,
        after.status,
        after.status == "applied",
        False,
        "failed",
        after.detail,
    )


def _has_markers(path: Path, *markers: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        # Partial checkout: a missing file means the marker is absent, so
        # patch_status degrades to missing/partial instead of crashing.
        return False
    return all(marker in text for marker in markers)


def _plugins_applied(root: Path) -> bool:
    return _has_markers(root / "hermes_cli" / "plugins.py", 'deliver: str = "output"', '"deliver": deliver_mode')


def _cli_applied(root: Path) -> bool:
    return _has_markers(root / "cli.py", 'entry.get("deliver") == "agent"', "_pending_input.put(str(result))")


def _gateway_dispatch_applied(root: Path) -> bool:
    return _has_markers(root / "tui_gateway" / "server.py", '"type": "send"', 'entry.get("deliver") == "agent"')


def _gateway_slash_exec_applied(root: Path) -> bool:
    return _has_markers(root / "tui_gateway" / "server.py", "agent-deliver command: use command.dispatch")


def _apply_via_git_branch(root: Path) -> bool:
    if not (root / ".git").is_dir():
        return False
    try:
        subprocess.run(
            ["git", "rev-parse", "--verify", BRANCH_NAME],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        subprocess.run(
            ["git", "checkout", BRANCH_NAME],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        return True
    except (subprocess.CalledProcessError, OSError):
        return False


def _apply_via_git_patch(root: Path) -> bool:
    if not (root / ".git").is_dir():
        return False
    try:
        patch_bytes = resources.files("cluxion_agentplugin_preprocessing").joinpath(PATCH_RESOURCE).read_bytes()
    except (FileNotFoundError, ModuleNotFoundError):
        return False
    with tempfile.NamedTemporaryFile("wb", suffix=".patch", delete=False) as handle:
        handle.write(patch_bytes)
        patch_path = Path(handle.name)
    try:
        subprocess.run(
            ["git", "apply", "--check", str(patch_path)],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        subprocess.run(
            ["git", "apply", str(patch_path)],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        return True
    except (subprocess.CalledProcessError, OSError):
        return False
    finally:
        patch_path.unlink(missing_ok=True)


def _timeout_detail(exc: subprocess.TimeoutExpired) -> str:
    cmd = exc.cmd if isinstance(exc.cmd, str) else " ".join(str(part) for part in exc.cmd)
    return f"{cmd} timed out after {exc.timeout or _GIT_TIMEOUT_SECONDS:g}s"


def _apply_inline(root: Path) -> bool:
    try:
        changed = False
        changed |= _patch_plugins_py(root)
        changed |= _patch_cli_py(root)
        changed |= _patch_tui_gateway(root)
        return changed
    except OSError:
        return False


def _backup(path: Path) -> None:
    if not path.exists():
        return
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    shutil.copy2(path, path.with_name(f"{path.name}.bak-cluxion-{stamp}"))


def _patch_plugins_py(root: Path) -> bool:
    path = root / "hermes_cli" / "plugins.py"
    text = path.read_text(encoding="utf-8")
    if _plugins_applied(root):
        return False
    if 'args_hint: str = "",\n    ) -> None:' not in text:
        return False
    old = (
        "        self._manager._plugin_commands[clean] = {\n"
        '            "handler": handler,\n'
        '            "description": description or "Plugin command",\n'
        '            "plugin": self.manifest.name,\n'
        '            "args_hint": (args_hint or "").strip(),\n'
        "        }"
    )
    new = (
        '        deliver_mode = (deliver or "output").strip().lower()\n'
        '        if deliver_mode not in {"output", "agent"}:\n'
        '            deliver_mode = "output"\n'
        "        self._manager._plugin_commands[clean] = {\n"
        '            "handler": handler,\n'
        '            "description": description or "Plugin command",\n'
        '            "plugin": self.manifest.name,\n'
        '            "args_hint": (args_hint or "").strip(),\n'
        '            "deliver": deliver_mode,\n'
        "        }"
    )
    if old not in text:
        return False
    _backup(path)
    text = text.replace(
        'args_hint: str = "",\n    ) -> None:',
        'args_hint: str = "",\n        deliver: str = "output",\n    ) -> None:',
        1,
    )
    text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")
    return True


def _patch_cli_py(root: Path) -> bool:
    path = root / "cli.py"
    text = path.read_text(encoding="utf-8")
    if _cli_applied(root):
        return False
    old = (
        "                    try:\n"
        "                        result = resolve_plugin_command_result(\n"
        "                            plugin_handler(user_args)\n"
        "                        )\n"
        "                        if result:\n"
        "                            _cprint(str(result))"
    )
    new = (
        "                    try:\n"
        "                        from hermes_cli.plugins import get_plugin_commands\n"
        "\n"
        '                        cmd_key = base_cmd.lstrip("/")\n'
        "                        entry = get_plugin_commands().get(cmd_key, {})\n"
        "                        result = resolve_plugin_command_result(\n"
        "                            plugin_handler(user_args)\n"
        "                        )\n"
        "                        if (\n"
        '                            entry.get("deliver") == "agent"\n'
        "                            and user_args.strip()\n"
        "                            and result\n"
        '                            and hasattr(self, "_pending_input")\n'
        "                        ):\n"
        "                            _cprint(\n"
        '                                f"\\n⚡ /{cmd_key} → agent "\n'
        '                                f"({len(str(result))} chars)"\n'
        "                            )\n"
        "                            self._pending_input.put(str(result))\n"
        "                        elif result:\n"
        "                            _cprint(str(result))"
    )
    if old not in text:
        return False
    _backup(path)
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return True


def _patch_tui_gateway(root: Path) -> bool:
    path = root / "tui_gateway" / "server.py"
    text = path.read_text(encoding="utf-8")
    changed = False
    if not _gateway_dispatch_applied(root):
        old = (
            "        if handler:\n"
            "            result = resolve_plugin_command_result(handler(arg))\n"
            '            return _ok(rid, {"type": "plugin", "output": str(result or "")})'
        )
        new = (
            "        if handler:\n"
            "            from hermes_cli.plugins import get_plugin_commands\n"
            "\n"
            "            result = resolve_plugin_command_result(handler(arg))\n"
            "            entry = get_plugin_commands().get(name, {})\n"
            '            if entry.get("deliver") == "agent" and arg.strip() and result:\n'
            '                return _ok(rid, {"type": "send", "message": str(result)})\n'
            '            return _ok(rid, {"type": "plugin", "output": str(result or "")})'
        )
        if old in text:
            _backup(path)
            text = text.replace(old, new, 1)
            changed = True
    if not _gateway_slash_exec_applied(root):
        old = (
            "    if plugin_handler and resolve_plugin_command_result:\n"
            "        try:\n"
            "            result = resolve_plugin_command_result(plugin_handler(_cmd_arg))\n"
            '            return _ok(rid, {"output": str(result or "(no output)")})'
        )
        new = (
            "    if plugin_handler and resolve_plugin_command_result:\n"
            "        try:\n"
            "            from hermes_cli.plugins import get_plugin_commands\n"
            "\n"
            "            entry = get_plugin_commands().get(_cmd_base, {})\n"
            '            if entry.get("deliver") == "agent":\n'
            "                return _err(\n"
            "                    rid,\n"
            "                    4018,\n"
            '                    f"agent-deliver command: use command.dispatch for /{_cmd_base}",\n'
            "                )\n"
            "            result = resolve_plugin_command_result(plugin_handler(_cmd_arg))\n"
            '            return _ok(rid, {"output": str(result or "(no output)")})'
        )
        if old in text:
            if not changed:
                _backup(path)
            text = text.replace(old, new, 1)
            changed = True
    if changed:
        path.write_text(text, encoding="utf-8")
    return changed


__all__ = [
    "PatchResult",
    "ensure_applied",
    "patch_status",
    "resolve_hermes_agent_root",
]
