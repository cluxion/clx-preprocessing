use std::fs::{self, OpenOptions};
use std::path::{Path, PathBuf};

use fs2::FileExt;
use serde_json::{json, Value};

use crate::types::{ok_payload, require_str, QueueError};

pub fn persist_bundle(store_dir: &Path, payload: &Value) -> Result<Value, QueueError> {
    let work_id = require_str(payload, "work_id")?;
    let bundle = payload
        .get("bundle")
        .cloned()
        .ok_or_else(|| QueueError::Usage("missing bundle".into()))?;
    let dispatch_dir = dispatch_dir(store_dir);
    fs::create_dir_all(&dispatch_dir)?;
    let path = bundle_path(&dispatch_dir, work_id)?;
    with_dispatch_lock(&dispatch_dir, || {
        write_atomic_json(&path, &bundle)?;
        Ok(ok_payload(json!({
            "stored": true,
            "path": path.to_string_lossy(),
        })))
    })
}

pub fn next_step(store_dir: &Path, payload: &Value) -> Result<Value, QueueError> {
    let work_id = require_str(payload, "work_id")?;
    let dispatch_dir = dispatch_dir(store_dir);
    fs::create_dir_all(&dispatch_dir)?;
    let path = bundle_path(&dispatch_dir, work_id)?;
    with_dispatch_lock(&dispatch_dir, || {
        let mut bundle = read_bundle(&path)?;
        let mut selected: Option<Value> = None;
        {
            let steps = steps_mut(&mut bundle)?;
            for step in steps.iter_mut() {
                let status = step.get("status").and_then(Value::as_str).unwrap_or("");
                if status == "queued" || status == "retry_wait" {
                    step["status"] = json!("running");
                    step["updated_at"] = json!(now_secs());
                    selected = Some(public_step(step));
                    break;
                }
            }
        }
        if let Some(step) = selected {
            write_atomic_json(&path, &bundle)?;
            let remaining = remaining_count(steps_ref(&bundle)?);
            return Ok(ok_payload(json!({
                "work_id": work_id,
                "ready": true,
                "step": step,
                "remaining": remaining,
                "synthesis_ready": false,
            })));
        }
        let steps = steps_ref(&bundle)?;
        Ok(ok_payload(json!({
            "work_id": work_id,
            "ready": false,
            "step": json!({}),
            "remaining": remaining_count(steps),
            "synthesis_ready": steps.iter().all(|step| step.get("status") == Some(&json!("succeeded"))),
        })))
    })
}

pub fn record_step(store_dir: &Path, payload: &Value) -> Result<Value, QueueError> {
    let work_id = require_str(payload, "work_id")?;
    let step_id = require_str(payload, "step_id")?;
    let result = payload.get("result").and_then(Value::as_str).unwrap_or("");
    let error = payload.get("error").and_then(Value::as_str).unwrap_or("");
    let failed = payload
        .get("failed")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let dispatch_dir = dispatch_dir(store_dir);
    fs::create_dir_all(&dispatch_dir)?;
    let path = bundle_path(&dispatch_dir, work_id)?;
    with_dispatch_lock(&dispatch_dir, || {
        let mut bundle = read_bundle(&path)?;
        let mut recorded_status = None;
        {
            let steps = steps_mut(&mut bundle)?;
            for step in steps.iter_mut() {
                if step.get("step_id") == Some(&json!(step_id)) {
                    step["status"] = json!(if failed { "failed" } else { "succeeded" });
                    step["result"] = json!(result);
                    step["error"] = json!(error);
                    step["updated_at"] = json!(now_secs());
                    recorded_status = step.get("status").cloned();
                    break;
                }
            }
        }
        if let Some(status) = recorded_status {
            write_atomic_json(&path, &bundle)?;
            let steps = steps_ref(&bundle)?;
            return Ok(ok_payload(json!({
                "work_id": work_id,
                "step_id": step_id,
                "recorded": true,
                "status": status,
                "remaining": remaining_count(steps),
                "synthesis_ready": steps.iter().all(|item| item.get("status") == Some(&json!("succeeded"))),
            })));
        }
        Err(QueueError::Store(format!(
            "dispatch step not found: {work_id}/{step_id}"
        )))
    })
}

pub fn build_brief(store_dir: &Path, payload: &Value) -> Result<Value, QueueError> {
    let work_id = require_str(payload, "work_id")?;
    let path = bundle_path(&dispatch_dir(store_dir), work_id)?;
    let bundle = read_bundle(&path)?;
    let steps = steps_ref(&bundle)?;
    let missing: Vec<String> = steps
        .iter()
        .filter(|step| step.get("status") != Some(&json!("succeeded")))
        .filter_map(|step| {
            step.get("step_id")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .collect();
    if !missing.is_empty() {
        return Ok(ok_payload(json!({
            "work_id": work_id,
            "ready": false,
            "missing_steps": missing,
            "briefing_prompt": "",
        })));
    }
    let prompt = briefing_prompt(&bundle, steps);
    Ok(ok_payload(json!({
        "work_id": work_id,
        "ready": true,
        "missing_steps": [],
        "briefing_prompt": prompt,
        "result_count": steps.len(),
    })))
}

fn with_dispatch_lock<T>(
    dispatch_dir: &Path,
    f: impl FnOnce() -> Result<T, QueueError>,
) -> Result<T, QueueError> {
    let lock_path = dispatch_dir.join(".dispatch.lock");
    let lockfile = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .open(&lock_path)?;
    lockfile.lock_exclusive()?;
    let result = f();
    let _ = lockfile.unlock();
    result
}

fn dispatch_dir(store_dir: &Path) -> PathBuf {
    store_dir.join("dispatch")
}

fn bundle_path(dispatch_dir: &Path, work_id: &str) -> Result<PathBuf, QueueError> {
    let safe: String = work_id
        .chars()
        .filter(|ch| ch.is_alphanumeric() || *ch == '-' || *ch == '_')
        .collect();
    if safe.is_empty() {
        return Err(QueueError::Usage("work_id is empty".into()));
    }
    Ok(dispatch_dir.join(format!("{safe}.json")))
}

fn read_bundle(path: &Path) -> Result<Value, QueueError> {
    if !path.exists() {
        return Err(QueueError::Store(format!(
            "dispatch bundle not found: {}",
            path.display()
        )));
    }
    let raw = fs::read_to_string(path)?;
    Ok(serde_json::from_str(&raw)?)
}

fn steps_mut(bundle: &mut Value) -> Result<&mut Vec<Value>, QueueError> {
    bundle
        .as_object_mut()
        .and_then(|obj| obj.get_mut("steps"))
        .and_then(Value::as_array_mut)
        .ok_or_else(|| QueueError::Store("dispatch bundle has no steps array".into()))
}

fn steps_ref(bundle: &Value) -> Result<&Vec<Value>, QueueError> {
    bundle
        .get("steps")
        .and_then(Value::as_array)
        .ok_or_else(|| QueueError::Store("dispatch bundle has no steps array".into()))
}

fn public_step(step: &Value) -> Value {
    json!({
        "step_id": step.get("step_id").cloned().unwrap_or(json!("")),
        "segment_id": step.get("segment_id").cloned().unwrap_or(json!("")),
        "checksum": step.get("checksum").cloned().unwrap_or(json!("")),
        "token_estimate": step.get("token_estimate").cloned().unwrap_or(json!(0)),
        "content": step.get("content").cloned().unwrap_or(json!("")),
        "instruction": "Process this segment with the current host model. Preserve checksum and do not claim checks were run unless they were run.",
    })
}

fn remaining_count(steps: &[Value]) -> i64 {
    steps
        .iter()
        .filter(|step| {
            matches!(
                step.get("status").and_then(Value::as_str),
                Some("queued") | Some("retry_wait") | Some("running")
            )
        })
        .count() as i64
}

fn briefing_prompt(bundle: &Value, steps: &[Value]) -> String {
    let mut lines = vec![
        "[cluxion_final_briefing]".into(),
        format!(
            "work_id={}",
            bundle.get("work_id").and_then(Value::as_str).unwrap_or("")
        ),
        "Synthesize the ordered segment results into a concise user-facing briefing.".into(),
        "Separate verified facts, tool results, inferences, missing checks, and remaining risks."
            .into(),
        "[segment_results]".into(),
    ];
    for step in steps {
        lines.push(format!(
            "step_id={}\nsegment_id={}\nchecksum={}\n{}",
            step.get("step_id").and_then(Value::as_str).unwrap_or(""),
            step.get("segment_id").and_then(Value::as_str).unwrap_or(""),
            step.get("checksum").and_then(Value::as_str).unwrap_or(""),
            step.get("result").and_then(Value::as_str).unwrap_or(""),
        ));
    }
    lines.join("\n\n")
}

fn write_atomic_json(path: &Path, payload: &Value) -> Result<(), QueueError> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let serialized = serde_json::to_string_pretty(payload)?;
    let temp = path.with_extension("json.tmp");
    fs::write(&temp, format!("{serialized}\n"))?;
    fs::rename(temp, path)?;
    Ok(())
}

fn now_secs() -> f64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or(0.0)
}
