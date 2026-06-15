"""CLI helpers for the universal Cluxion preprocessing plugin."""

from __future__ import annotations

import argparse
import importlib.resources
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from cluxion_agentplugin_preprocessing import __version__, hermes_config, runner
from cluxion_agentplugin_preprocessing.doctor import (
    DoctorResult,
    render_json,
    render_text,
    run_doctor,
)
from cluxion_runtime.bootstrap import ensure_local_runtime

if TYPE_CHECKING:
    from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Run cluxion-preprocess CLI."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "check":
        return _check()
    if args.command == "bootstrap":
        return _bootstrap(args)
    if args.command == "status":
        return _status(args)
    if args.command == "enable":
        return _enable(args)
    if args.command == "disable":
        return _disable(args)
    if args.command == "hermes-config":
        return _hermes_config(args)
    if args.command == "doctor":
        return _doctor(args)
    parser.print_help(sys.stderr)
    return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cluxion-preprocess")
    parser.add_argument(
        "--version", action="version", version=f"cluxion-agentplugin-preprocessing {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("check", help="Check whether cluxion-runtime is visible")
    bootstrap = subparsers.add_parser(
        "bootstrap", help="Install or upgrade local runtime dependencies"
    )
    bootstrap.add_argument("--upgrade", action="store_true")
    bootstrap.add_argument("--dry-run", action="store_true")
    bootstrap.add_argument("--package", action="append", default=None)
    status = subparsers.add_parser(
        "status", help="Show whether Hermes config enables this plugin"
    )
    status.add_argument("--home", default=None)
    enable = subparsers.add_parser(
        "enable", help="Enable the pip-installed plugin in Hermes config"
    )
    enable.add_argument("--home", default=None)
    enable.add_argument("--dry-run", action="store_true")
    disable = subparsers.add_parser(
        "disable", help="Disable the pip-installed plugin in Hermes config"
    )
    disable.add_argument("--home", default=None)
    disable.add_argument("--dry-run", action="store_true")
    config = subparsers.add_parser(
        "hermes-config",
        help="Render Hermes local endpoint config through cluxion-runtime",
    )
    config.add_argument("--model", required=True)
    config.add_argument("--host", default="127.0.0.1")
    config.add_argument("--port", type=int, default=23003)
    config.add_argument("--context-length", type=int, default=131_072)
    config.add_argument("--provider-key", default="cluxion-local")
    config.add_argument("--display-name", default="Cluxion Local")
    doctor = subparsers.add_parser("doctor", help="Run embedded health checks")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--verbose", action="store_true")
    return parser


def _check() -> int:
    from cluxion_runtime.resources.queue_bridge import queue_available

    payload = {
        "plugin": "cluxion-agentplugin-preprocessing",
        "version": __version__,
        "cluxion_runtime_available": runner.runtime_available(),
        "rust_queue_available": queue_available(),
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["cluxion_runtime_available"] else 1


def _bootstrap(args: argparse.Namespace) -> int:
    result = ensure_local_runtime(
        packages=tuple(args.package) if args.package else ("vllm-mlx",),
        upgrade=bool(args.upgrade),
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0 if result.ok else 1


def _status(args: argparse.Namespace) -> int:
    result = hermes_config.plugin_status(args.home)
    print(json.dumps({"ok": True, **result.to_dict()}, ensure_ascii=False, sort_keys=True))
    return 0 if result.enabled else 1


def _enable(args: argparse.Namespace) -> int:
    result = hermes_config.enable_plugin(args.home, dry_run=bool(args.dry_run))
    print(json.dumps({"ok": True, **result.to_dict()}, ensure_ascii=False, sort_keys=True))
    return 0


def _disable(args: argparse.Namespace) -> int:
    result = hermes_config.disable_plugin(args.home, dry_run=bool(args.dry_run))
    print(json.dumps({"ok": True, **result.to_dict()}, ensure_ascii=False, sort_keys=True))
    return 0


def _hermes_config(args: argparse.Namespace) -> int:
    result = runner.hermes_config(
        {
            "model": str(args.model),
            "host": str(args.host),
            "port": int(args.port),
            "context_length": int(args.context_length),
            "provider_key": str(args.provider_key),
            "display_name": str(args.display_name),
        }
    )
    print(result.to_json())
    return 0 if result.ok else 1


def _doctor(args: argparse.Namespace) -> int:
    pkg = "cluxion_agentplugin_preprocessing.doctor"
    catalog_path = Path(str(importlib.resources.files(pkg).joinpath("catalog.json")))
    result = run_doctor(
        cwd=Path.cwd(),
        hermes_bin="hermes",
        catalog_path=catalog_path,
        probes=__import__(
            "cluxion_agentplugin_preprocessing.doctor.probes", fromlist=["PROBES"]
        ).PROBES,
        plugin="preprocessing",
        version=__version__,
    )
    text = render_text(result, __import__(
        "cluxion_agentplugin_preprocessing.doctor.framework", fromlist=["load_catalog"]
    ).load_catalog(catalog_path), verbose=bool(args.verbose))
    print(text, file=sys.stderr)
    if args.json:
        print(render_json(result))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
