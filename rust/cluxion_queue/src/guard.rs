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
pub const DEFAULT_DAEMON_INTERVAL_MS: u64 = 200;
pub const DEFAULT_DAEMON_WINDOW: usize = 25;
pub const STATE_FILE_NAME: &str = "guard_state.json";

fn epoch_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
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

fn sample_from(sys: &System) -> Value {
    let mut zombie_pids: Vec<u64> = sys
        .processes()
        .iter()
        .filter(|(_, proc_)| proc_.status() == ProcessStatus::Zombie)
        .map(|(pid, _)| pid.as_u32() as u64)
        .collect();
    zombie_pids.sort_unstable();
    let zombie_count = zombie_pids.len();
    zombie_pids.truncate(MAX_REPORTED_PIDS);
    json!({
        "ok": true,
        "total_ram_mb": sys.total_memory() / 1_048_576,
        "available_ram_mb": sys.available_memory() / 1_048_576,
        "swap_used_mb": sys.used_swap() / 1_048_576,
        "cpu_percent": f64::from(sys.global_cpu_usage()),
        "process_count": sys.processes().len(),
        "zombie_count": zombie_count,
        "zombie_pids": zombie_pids,
        "sampled_at_ms": epoch_ms(),
    })
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
/// atomically (write to a temp file, then rename). Runs until killed.
pub fn run_daemon(store_dir: &Path, interval_ms: u64, window: usize) -> Result<(), QueueError> {
    std::fs::create_dir_all(store_dir)?;
    let state_path = store_dir.join(STATE_FILE_NAME);
    let tmp_path = store_dir.join(format!("{STATE_FILE_NAME}.tmp"));
    let interval = std::time::Duration::from_millis(interval_ms.max(100));
    let window = window.max(1);

    let mut sys = System::new();
    sys.refresh_memory();
    sys.refresh_cpu_usage();
    let mut cpu_window: Vec<f64> = Vec::with_capacity(window);
    let mut ram_window: Vec<u64> = Vec::with_capacity(window);

    loop {
        std::thread::sleep(interval);
        sys.refresh_memory();
        sys.refresh_cpu_usage();
        sys.refresh_processes(ProcessesToUpdate::All, true);
        let current = sample_from(&sys);

        let cpu = current["cpu_percent"].as_f64().unwrap_or(0.0);
        let ram = current["available_ram_mb"].as_u64().unwrap_or(0);
        if cpu_window.len() == window {
            cpu_window.remove(0);
            ram_window.remove(0);
        }
        cpu_window.push(cpu);
        ram_window.push(ram);

        let state = json!({
            "ok": true,
            "current": current,
            "window": {
                "samples": cpu_window.len(),
                "cpu_avg": cpu_window.iter().sum::<f64>() / cpu_window.len() as f64,
                "cpu_peak": cpu_window.iter().cloned().fold(0.0, f64::max),
                "min_available_ram_mb": ram_window.iter().min().copied().unwrap_or(0),
            },
            "interval_ms": interval.as_millis() as u64,
            "updated_at_ms": epoch_ms(),
        });
        std::fs::write(&tmp_path, serde_json::to_vec(&state)?)?;
        std::fs::rename(&tmp_path, &state_path)?;
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
}
