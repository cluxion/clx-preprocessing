"""Blocking guard daemon entry point for PyPI wheels without the CLI binary."""

from __future__ import annotations

import json
import sys


def main() -> int:
    if len(sys.argv) != 4:
        print(json.dumps({"ok": False, "error": "usage"}), file=sys.stderr)
        return 1
    _, store_dir, interval_ms, window = sys.argv
    try:
        import cluxion_queue_native
    except ImportError:
        print(json.dumps({"ok": False, "error": "native_module_missing"}), file=sys.stderr)
        return 1
    run_guard_daemon = getattr(cluxion_queue_native, "run_guard_daemon", None)
    if run_guard_daemon is None:
        print(json.dumps({"ok": False, "error": "run_guard_daemon_missing"}), file=sys.stderr)
        return 1
    run_guard_daemon(store_dir, int(interval_ms), int(window))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
