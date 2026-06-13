//! Cluxion queue core: shared by the `cluxion-queue` CLI binary and the
//! optional PyO3 extension module (`python` feature). One JSON-in/JSON-out
//! entry point keeps CLI and in-process semantics identical.

pub mod context;
pub mod dispatch;
pub mod guard;
pub mod queue;
pub mod types;

use std::path::PathBuf;

use serde_json::Value;
use types::QueueError;

/// Route a command to the queue/dispatch implementation.
pub fn run_command(command: &str, payload: &Value) -> Result<Value, QueueError> {
    // Pure-function commands resolve before store_dir: no filesystem involved.
    if command == "context-compress" {
        return context::compress(payload);
    }
    if command == "guard-sample" {
        return guard::sample(payload);
    }
    if command == "guard-scan" {
        return guard::scan(payload);
    }
    let store_dir = store_dir_from_payload(payload)?;
    match command {
        "enqueue" => queue::enqueue(&store_dir, payload),
        "dequeue" => queue::dequeue(&store_dir, payload),
        "peek" => queue::peek(&store_dir, payload),
        "persist" => dispatch::persist_bundle(&store_dir, payload),
        "next" => dispatch::next_step(&store_dir, payload),
        "record" => dispatch::record_step(&store_dir, payload),
        "brief" => dispatch::build_brief(&store_dir, payload),
        "status" => queue::status(&store_dir, payload),
        other => Err(QueueError::Usage(format!("unknown command: {other}"))),
    }
}

pub fn store_dir_from_payload(payload: &Value) -> Result<PathBuf, QueueError> {
    if let Some(path) = payload.get("store_dir").and_then(Value::as_str) {
        if !path.is_empty() {
            return Ok(PathBuf::from(path));
        }
    }
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".into());
    Ok(PathBuf::from(home)
        .join(".local")
        .join("share")
        .join("cluxion-agentplugin-preprocessing")
        .join("queue"))
}

#[cfg(feature = "python")]
mod python_module {
    use pyo3::exceptions::{PyRuntimeError, PyValueError};
    use pyo3::prelude::*;

    /// Run a queue command in-process. Returns the result as a JSON string.
    #[pyfunction]
    fn run(command: &str, payload_json: &str) -> PyResult<String> {
        let payload: serde_json::Value = serde_json::from_str(payload_json)
            .map_err(|err| PyValueError::new_err(format!("invalid payload JSON: {err}")))?;
        let result = super::run_command(command, &payload)
            .map_err(|err| PyRuntimeError::new_err(err.to_string()))?;
        serde_json::to_string(&result)
            .map_err(|err| PyRuntimeError::new_err(format!("result serialization failed: {err}")))
    }

    /// Blocking guard daemon loop (same as the CLI ``guard-daemon`` subcommand).
    #[pyfunction]
    fn run_guard_daemon(store_dir: &str, interval_ms: u64, window: usize) -> PyResult<()> {
        super::guard::run_daemon(std::path::Path::new(store_dir), interval_ms, window)
            .map_err(|err| PyRuntimeError::new_err(err.to_string()))?;
        Ok(())
    }

    #[pymodule]
    fn cluxion_queue_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add_function(wrap_pyfunction!(run, m)?)?;
        m.add_function(wrap_pyfunction!(run_guard_daemon, m)?)?;
        m.add("__backend__", "rust_native")?;
        Ok(())
    }
}
