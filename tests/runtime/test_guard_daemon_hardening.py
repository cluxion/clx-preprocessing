from __future__ import annotations

from pathlib import Path

from cluxion_runtime import guard_daemon_host as gd


def test_process_scan_throttled_by_wall_clock(monkeypatch) -> None:
    calls = {"scan": 0}

    def _fake_scan() -> gd.ProcessScanCache:
        calls["scan"] += 1
        return gd.ProcessScanCache(process_count=1, zombie_count=0, zombie_pids=[], scanned_at_ms=gd._epoch_ms())

    monkeypatch.setattr(gd, "_scan_process_fields", _fake_scan)
    cache = gd.ProcessScanCache(process_count=0, zombie_count=0, zombie_pids=[])
    for tick in range(20):
        _, cache = gd._python_daemon_tick(cache, [], [], 5, 100, tick)
    assert calls["scan"] == 1, "scans within the 5s window must reuse the cache"


def test_missing_heartbeat_idles_out_from_daemon_start(tmp_path: Path) -> None:
    started = gd._epoch_ms() - 10_000
    assert gd._check_idle_exit(tmp_path, idle_ttl_ms=5_000, started_ms=started) is True
    assert gd._check_idle_exit(tmp_path, idle_ttl_ms=60_000, started_ms=gd._epoch_ms()) is False


def test_state_write_failures_terminate_daemon(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(gd, "_idle_ttl_ms", lambda: 10_000_000)
    monkeypatch.setattr(gd.time, "sleep", lambda _s: None)

    def _boom(*args: object, **kwargs: object):
        raise OSError("disk gone")

    monkeypatch.setattr(gd, "_daemon_loop_step", _boom)
    gd._run_python_daemon(str(tmp_path), 100, 5)
    err = capsys.readouterr().err
    assert "state writes failing" in err
