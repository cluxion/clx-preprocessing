//! Real-time resource guard: system sampling, process scanning with a
//! fail-closed ownership gate, and a polling daemon that publishes rolling
//! state for zero-cost reads from Python.
//!
//! Report-only by design: the guard never kills anything. Ownership is
//! decided by walking parent lineage up to registered root PIDs; any
//! process whose lineage cannot be proven is reported as external.

use std::collections::HashMap;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};
use sysinfo::{ProcessStatus, ProcessesToUpdate, System};

use crate::types::QueueError;

const DEFAULT_CPU_SAMPLE_MS: u64 = 100;
const MAX_REPORTED_PIDS: usize = 50;
const DEFAULT_CPU_HOT_THRESHOLD: f64 = 50.0;
const DEFAULT_RSS_HOT_THRESHOLD_MB: u64 = 1024;
pub const DEFAULT_DAEMON_INTERVAL_MS: u64 = 1000;
pub const DEFAULT_DAEMON_WINDOW: usize = 10;
pub const PROC_SCAN_EVERY_N_TICKS: u64 = 5;
/// Full process scans cost ~90ms on busy hosts; throttle them by wall clock
/// so the daemon stays cheap at any interval_ms.
pub const PROC_SCAN_MIN_INTERVAL_MS: u64 = 5_000;
pub const STATE_FILE_NAME: &str = "guard_state.json";
pub const HEARTBEAT_FILE_NAME: &str = "guard_heartbeat";
pub const PID_FILE_NAME: &str = "guard_daemon.pid";
pub const DEFAULT_IDLE_TTL_MS: u64 = 600_000;

fn epoch_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

fn idle_ttl_ms() -> u64 {
    std::env::var("CLUXION_GUARD_IDLE_TTL_MS")
        .ok()
        .and_then(|raw| raw.parse::<u64>().ok())
        .unwrap_or(DEFAULT_IDLE_TTL_MS)
}

/// True when the heartbeat mtime is older than `ttl_ms` relative to `now_ms`.
pub fn is_idle(heartbeat_mtime_ms: u64, now_ms: u64, ttl_ms: u64) -> bool {
    now_ms.saturating_sub(heartbeat_mtime_ms) > ttl_ms
}

fn heartbeat_mtime_ms(path: &Path) -> Option<u64> {
    let modified = std::fs::metadata(path).ok()?.modified().ok()?;
    modified
        .duration_since(UNIX_EPOCH)
        .ok()
        .map(|duration| duration.as_millis() as u64)
}

fn uint_field(payload: &Value, key: &str, default: u64) -> u64 {
    payload.get(key).and_then(Value::as_u64).unwrap_or(default)
}

/// One full system sample. CPU usage needs two refreshes separated by a
/// short sleep; `cpu_sample_ms` controls that pause (clamped to >= 100ms,
/// sysinfo's minimum meaningful interval).
pub fn sample(payload: &Value) -> Result<Value, QueueError> {
    let cpu_sample_ms = uint_field(payload, "cpu_sample_ms", DEFAULT_CPU_SAMPLE_MS).max(100);
    let mut sys = System::new();
    sys.refresh_memory();
    sys.refresh_cpu_usage();
    std::thread::sleep(std::time::Duration::from_millis(cpu_sample_ms));
    sys.refresh_cpu_usage();
    sys.refresh_processes(ProcessesToUpdate::All, true);
    Ok(sample_from(&sys))
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct ProcessScanCache {
    process_count: usize,
    zombie_count: usize,
    zombie_pids: Vec<u64>,
    scanned_at_ms: u64,
}

fn scan_process_fields(sys: &System) -> ProcessScanCache {
    let mut zombie_pids: Vec<u64> = sys
        .processes()
        .iter()
        .filter(|(_, proc_)| proc_.status() == ProcessStatus::Zombie)
        .map(|(pid, _)| pid.as_u32() as u64)
        .collect();
    zombie_pids.sort_unstable();
    let zombie_count = zombie_pids.len();
    zombie_pids.truncate(MAX_REPORTED_PIDS);
    ProcessScanCache {
        process_count: sys.processes().len(),
        zombie_count,
        zombie_pids,
        scanned_at_ms: epoch_ms(),
    }
}

fn available_memory_bytes(sys: &System) -> u64 {
    // sysinfo's available_memory() can report 0 on macOS hosts; a guard that
    // believes memory is exhausted would misjudge every RAM-floor decision,
    // so fall back to total-used when the direct reading is missing.
    let direct = sys.available_memory();
    if direct > 0 {
        return direct;
    }
    sys.total_memory().saturating_sub(sys.used_memory())
}

fn build_current_snapshot(sys: &System, process_cache: &ProcessScanCache) -> Value {
    json!({
        "ok": true,
        "total_ram_mb": sys.total_memory() / 1_048_576,
        "available_ram_mb": available_memory_bytes(sys) / 1_048_576,
        "swap_used_mb": sys.used_swap() / 1_048_576,
        "cpu_percent": f64::from(sys.global_cpu_usage()),
        "process_count": process_cache.process_count,
        "zombie_count": process_cache.zombie_count,
        "zombie_pids": process_cache.zombie_pids.clone(),
        "sampled_at_ms": epoch_ms(),
    })
}

fn sample_from(sys: &System) -> Value {
    build_current_snapshot(sys, &scan_process_fields(sys))
}

fn push_window_sample(
    cpu_window: &mut Vec<f64>,
    ram_window: &mut Vec<u64>,
    window: usize,
    cpu: f64,
    ram: u64,
) {
    if cpu_window.len() == window {
        cpu_window.remove(0);
        ram_window.remove(0);
    }
    cpu_window.push(cpu);
    ram_window.push(ram);
}

fn build_daemon_state(
    current: &Value,
    cpu_window: &[f64],
    ram_window: &[u64],
    interval_ms: u64,
) -> Value {
    json!({
        "ok": true,
        "current": current,
        "window": {
            "samples": cpu_window.len(),
            "cpu_avg": cpu_window.iter().sum::<f64>() / cpu_window.len() as f64,
            "cpu_peak": cpu_window.iter().cloned().fold(0.0, f64::max),
            "min_available_ram_mb": ram_window.iter().min().copied().unwrap_or(0),
        },
        "interval_ms": interval_ms,
        "updated_at_ms": epoch_ms(),
    })
}

fn daemon_tick(
    sys: &mut System,
    process_cache: &mut ProcessScanCache,
    cpu_window: &mut Vec<f64>,
    ram_window: &mut Vec<u64>,
    window: usize,
    interval_ms: u64,
    tick: u64,
) -> Value {
    let now = epoch_ms();
    let scan_due = process_cache.scanned_at_ms == 0
        || now.saturating_sub(process_cache.scanned_at_ms) >= PROC_SCAN_MIN_INTERVAL_MS;
    let _ = tick; // cadence is wall-clock based; tick retained for call compatibility
    if scan_due {
        sys.refresh_processes(ProcessesToUpdate::All, true);
        *process_cache = scan_process_fields(sys);
    }
    sys.refresh_memory();
    sys.refresh_cpu_usage();
    let current = build_current_snapshot(sys, process_cache);
    let cpu = current["cpu_percent"].as_f64().unwrap_or(0.0);
    let ram = current["available_ram_mb"].as_u64().unwrap_or(0);
    push_window_sample(cpu_window, ram_window, window, cpu, ram);
    build_daemon_state(&current, cpu_window, ram_window, interval_ms)
}

/// Scan processes against registered owner roots. A process is `owned`
/// only when its parent lineage reaches one of `owned_roots`; everything
/// else — including processes whose lineage cannot be walked — is
/// external and must only ever be reported, never acted on.
pub fn scan(payload: &Value) -> Result<Value, QueueError> {
    let owned_roots: Vec<u64> = payload
        .get("owned_roots")
        .and_then(Value::as_array)
        .map(|items| items.iter().filter_map(Value::as_u64).collect())
        .unwrap_or_default();
    let cpu_hot = payload
        .get("cpu_threshold")
        .and_then(Value::as_f64)
        .unwrap_or(DEFAULT_CPU_HOT_THRESHOLD);
    let rss_hot_mb = uint_field(payload, "rss_threshold_mb", DEFAULT_RSS_HOT_THRESHOLD_MB);

    let mut sys = System::new();
    sys.refresh_cpu_usage();
    std::thread::sleep(std::time::Duration::from_millis(DEFAULT_CPU_SAMPLE_MS));
    sys.refresh_processes(ProcessesToUpdate::All, true);

    let mut parents: HashMap<u64, u64> = HashMap::with_capacity(sys.processes().len());
    for (pid, proc_) in sys.processes() {
        if let Some(parent) = proc_.parent() {
            parents.insert(pid.as_u32() as u64, parent.as_u32() as u64);
        }
    }

    let mut zombies = Vec::new();
    let mut hot = Vec::new();
    let mut owned_alive = 0u64;
    for (pid, proc_) in sys.processes() {
        let pid_u = pid.as_u32() as u64;
        let owned = is_owned(pid_u, &owned_roots, &parents);
        if owned && proc_.status() != ProcessStatus::Zombie {
            owned_alive += 1;
        }
        let entry = || {
            json!({
                "pid": pid_u,
                "ppid": parents.get(&pid_u).copied(),
                "name": proc_.name().to_string_lossy(),
                "cpu_percent": f64::from(proc_.cpu_usage()),
                "rss_mb": proc_.memory() / 1_048_576,
                "owned": owned,
            })
        };
        if proc_.status() == ProcessStatus::Zombie {
            if zombies.len() < MAX_REPORTED_PIDS {
                zombies.push(entry());
            }
        } else if f64::from(proc_.cpu_usage()) >= cpu_hot || proc_.memory() / 1_048_576 >= rss_hot_mb {
            if hot.len() < MAX_REPORTED_PIDS {
                hot.push(entry());
            }
        }
    }
    Ok(json!({
        "ok": true,
        "owned_roots": owned_roots,
        "owned_alive": owned_alive,
        "zombies": zombies,
        "hot": hot,
        "scanned_at_ms": epoch_ms(),
    }))
}

fn is_owned(pid: u64, owned_roots: &[u64], parents: &HashMap<u64, u64>) -> bool {
    if owned_roots.is_empty() {
        return false;
    }
    let mut current = pid;
    // Bounded walk: cycles cannot occur in a ppid chain, but stay defensive.
    for _ in 0..64 {
        if owned_roots.contains(&current) {
            return true;
        }
        match parents.get(&current) {
            Some(&parent) if parent != current => current = parent,
            _ => return false,
        }
    }
    false
}

/// Polling daemon: refresh, fold into a rolling window, publish state
/// atomically (write to a temp file, then rename). Self-exits when the
/// heartbeat file is stale; otherwise runs until killed.
///
/// Cheap ticks refresh memory/CPU every `interval_ms`; full process scans run
/// every [`PROC_SCAN_EVERY_N_TICKS`] ticks and their results are cached for
/// intervening cheap ticks.
pub fn run_daemon(store_dir: &Path, interval_ms: u64, window: usize) -> Result<(), QueueError> {
    std::fs::create_dir_all(store_dir)?;
    let state_path = store_dir.join(STATE_FILE_NAME);
    let tmp_path = store_dir.join(format!("{STATE_FILE_NAME}.tmp"));
    let heartbeat_path = store_dir.join(HEARTBEAT_FILE_NAME);
    let pid_path = store_dir.join(PID_FILE_NAME);
    let idle_ttl = idle_ttl_ms();
    let interval = std::time::Duration::from_millis(interval_ms.max(100));
    let window = window.max(1);
    let interval_ms = interval.as_millis() as u64;

    let mut sys = System::new();
    let mut process_cache = ProcessScanCache {
        process_count: 0,
        zombie_count: 0,
        zombie_pids: Vec::new(),
        scanned_at_ms: 0,
    };
    let mut cpu_window: Vec<f64> = Vec::with_capacity(window);
    let mut ram_window: Vec<u64> = Vec::with_capacity(window);
    let mut tick: u64 = 0;
    let started_ms = epoch_ms();

    loop {
        // A missing heartbeat means no client ever attached; measuring
        // idleness from daemon start prevents immortal orphans (a live one
        // burned 49 CPU-minutes when this only checked existing heartbeats).
        let last_activity = heartbeat_mtime_ms(&heartbeat_path).unwrap_or(started_ms);
        if is_idle(last_activity, epoch_ms(), idle_ttl) {
            let _ = std::fs::remove_file(&pid_path);
            return Ok(());
        }
        let state = daemon_tick(
            &mut sys,
            &mut process_cache,
            &mut cpu_window,
            &mut ram_window,
            window,
            interval_ms,
            tick,
        );
        std::fs::write(&tmp_path, serde_json::to_vec(&state)?)?;
        std::fs::rename(&tmp_path, &state_path)?;
        tick += 1;
        std::thread::sleep(interval);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn sample_reports_memory_and_processes() {
        let result = sample(&json!({"cpu_sample_ms": 100})).expect("sample");
        assert!(result["total_ram_mb"].as_u64().unwrap() > 0);
        assert!(result["available_ram_mb"].as_u64().unwrap() > 0);
        assert!(result["process_count"].as_u64().unwrap() > 1);
        let cpu = result["cpu_percent"].as_f64().unwrap();
        assert!((0.0..=100.0 * 64.0).contains(&cpu));
    }

    #[test]
    fn scan_marks_own_lineage_and_external() {
        let me = std::process::id() as u64;
        let result = scan(&json!({
            "owned_roots": [me],
            "cpu_threshold": 0.0,
            "rss_threshold_mb": 0,
        }))
        .expect("scan");
        // Every process is reported as hot (zero thresholds); this test
        // process must be owned, PID 1 must be external.
        let hot = result["hot"].as_array().unwrap();
        assert!(!hot.is_empty());
        assert!(result["owned_alive"].as_u64().unwrap() >= 1);
        for entry in hot {
            if entry["pid"].as_u64() == Some(1) {
                assert_eq!(entry["owned"].as_bool(), Some(false));
            }
        }
    }

    #[test]
    fn scan_without_roots_owns_nothing() {
        let result = scan(&json!({"cpu_threshold": 0.0, "rss_threshold_mb": 0})).expect("scan");
        assert_eq!(result["owned_alive"].as_u64(), Some(0));
        for entry in result["hot"].as_array().unwrap() {
            assert_eq!(entry["owned"].as_bool(), Some(false));
        }
    }

    #[test]
    fn ownership_walk_is_fail_closed() {
        let mut parents = HashMap::new();
        parents.insert(10u64, 5u64);
        parents.insert(5u64, 1u64);
        assert!(is_owned(10, &[5], &parents));
        assert!(is_owned(10, &[1], &parents));
        assert!(!is_owned(10, &[99], &parents));
        // Unknown lineage (pid not in map) -> external.
        assert!(!is_owned(42, &[5], &parents));
        // No roots registered -> nothing is owned.
        assert!(!is_owned(10, &[], &parents));
    }

    #[test]
    fn daemon_tick_caches_process_fields_between_scans() {
        let mut sys = System::new();
        sys.refresh_processes(ProcessesToUpdate::All, true);
        let mut process_cache = scan_process_fields(&sys);
        let mut cpu_window: Vec<f64> = Vec::with_capacity(3);
        let mut ram_window: Vec<u64> = Vec::with_capacity(3);

        let scan_tick_state = daemon_tick(
            &mut sys,
            &mut process_cache,
            &mut cpu_window,
            &mut ram_window,
            3,
            1000,
            0,
        );
        let scan_process_count = scan_tick_state["current"]["process_count"]
            .as_u64()
            .expect("process_count");
        assert!(scan_process_count > 0);

        let stale_cache = ProcessScanCache {
            process_count: 1,
            zombie_count: 2,
            zombie_pids: vec![99, 100],
            scanned_at_ms: epoch_ms(),
        };
        process_cache = stale_cache.clone();

        let cheap_tick_state = daemon_tick(
            &mut sys,
            &mut process_cache,
            &mut cpu_window,
            &mut ram_window,
            3,
            1000,
            1,
        );
        assert_eq!(
            cheap_tick_state["current"]["process_count"].as_u64(),
            Some(1)
        );
        assert_eq!(
            cheap_tick_state["current"]["zombie_count"].as_u64(),
            Some(2)
        );
        assert_eq!(
            cheap_tick_state["current"]["zombie_pids"],
            json!([99, 100])
        );

        // Cadence is wall-clock based: age the cache past the scan window
        // to force a rescan regardless of tick number.
        process_cache.scanned_at_ms = epoch_ms().saturating_sub(PROC_SCAN_MIN_INTERVAL_MS + 1);
        let aged_cache = process_cache.clone();
        let rescan_tick_state = daemon_tick(
            &mut sys,
            &mut process_cache,
            &mut cpu_window,
            &mut ram_window,
            3,
            1000,
            PROC_SCAN_EVERY_N_TICKS,
        );
        assert_ne!(process_cache, aged_cache);
        assert!(process_cache.process_count > 0);
        assert_eq!(
            rescan_tick_state["current"]["process_count"].as_u64(),
            Some(process_cache.process_count as u64)
        );
    }

    #[test]
    fn is_idle_detects_stale_and_fresh_heartbeats() {
        let ttl = 600_000;
        let now = 1_700_000_000_000u64;
        assert!(is_idle(now - ttl - 1, now, ttl));
        assert!(!is_idle(now - ttl, now, ttl));
        assert!(!is_idle(now - 1, now, ttl));
    }


    #[test]
    fn daemon_without_heartbeat_idles_out_from_start() {
        let dir = std::env::temp_dir().join(format!("guard-idle-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::env::set_var("CLUXION_GUARD_IDLE_TTL_MS", "300");
        let started = std::time::Instant::now();
        let result = run_daemon(&dir, 100, 3);
        std::env::remove_var("CLUXION_GUARD_IDLE_TTL_MS");
        let _ = std::fs::remove_dir_all(&dir);
        assert!(result.is_ok());
        assert!(
            started.elapsed() < std::time::Duration::from_secs(5),
            "daemon must exit shortly after the idle TTL when no heartbeat ever appears"
        );
    }

    #[test]
    fn daemon_state_json_has_required_keys() {
        let mut sys = System::new();
        sys.refresh_processes(ProcessesToUpdate::All, true);
        let mut process_cache = scan_process_fields(&sys);
        let mut cpu_window: Vec<f64> = Vec::with_capacity(2);
        let mut ram_window: Vec<u64> = Vec::with_capacity(2);

        let state = daemon_tick(
            &mut sys,
            &mut process_cache,
            &mut cpu_window,
            &mut ram_window,
            2,
            1000,
            0,
        );

        for key in ["ok", "current", "window", "interval_ms", "updated_at_ms"] {
            assert!(state.get(key).is_some(), "missing top-level key: {key}");
        }
        for key in [
            "ok",
            "total_ram_mb",
            "available_ram_mb",
            "swap_used_mb",
            "cpu_percent",
            "process_count",
            "zombie_count",
            "zombie_pids",
            "sampled_at_ms",
        ] {
            assert!(
                state["current"].get(key).is_some(),
                "missing current key: {key}"
            );
        }
        for key in ["samples", "cpu_avg", "cpu_peak", "min_available_ram_mb"] {
            assert!(
                state["window"].get(key).is_some(),
                "missing window key: {key}"
            );
        }
    }
}
