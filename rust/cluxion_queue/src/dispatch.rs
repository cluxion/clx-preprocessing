use std::fs::{self, OpenOptions};
#[cfg(unix)]
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

use fs2::FileExt;
use serde_json::{json, Value};

use crate::types::{ok_payload, require_str, QueueError};

/// Mirrors dispatch_store.RUNNING_LEASE_SECONDS and py_queue._RUNNING_LEASE_SECONDS.
const RUNNING_LEASE_SECS: f64 = 600.0;

pub fn persist_bundle(store_dir: &Path, payload: &Value) -> Result<Value, QueueError> {
    let work_id = require_str(payload, "work_id")?;
    let bundle = payload
        .get("bundle")
        .cloned()
        .ok_or_else(|| QueueError::Usage("missing bundle".into()))?;
    let dispatch_dir = dispatch_dir(store_dir)?;
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
    let dispatch_dir = dispatch_dir(store_dir)?;
    let path = bundle_path(&dispatch_dir, work_id)?;
    let now = now_secs();
    with_dispatch_lock(&dispatch_dir, || {
        let mut bundle = read_bundle(&path)?;
        let mut selected: Option<Value> = None;
        {
            let steps = steps_mut(&mut bundle)?;
            for step in steps.iter_mut() {
                let status = step.get("status").and_then(Value::as_str).unwrap_or("");
                if status == "queued" || status == "retry_wait" || stale_running(step, now) {
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
    let retryable = payload
        .get("retryable")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let dispatch_dir = dispatch_dir(store_dir)?;
    let path = bundle_path(&dispatch_dir, work_id)?;
    with_dispatch_lock(&dispatch_dir, || {
        let mut bundle = read_bundle(&path)?;
        let mut recorded_status = None;
        {
            let steps = steps_mut(&mut bundle)?;
            for step in steps.iter_mut() {
                if step.get("step_id") == Some(&json!(step_id)) {
                    step["status"] = json!(if failed {
                        if retryable {
                            "retry_wait"
                        } else {
                            "failed"
                        }
                    } else {
                        "succeeded"
                    });
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
    let path = bundle_path(&dispatch_dir(store_dir)?, work_id)?;
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
    // Fail-closed: never open through a planted lock symlink.
    if lock_path.is_symlink() {
        return Err(QueueError::Store(format!(
            "expected regular file, found symlink: {}",
            lock_path.display()
        )));
    }
    match fs::symlink_metadata(&lock_path) {
        Ok(meta) if meta.is_file() => {}
        Ok(_) => {
            return Err(QueueError::Store(format!(
                "expected regular file, found non-regular: {}",
                lock_path.display()
            )));
        }
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
        Err(err) => return Err(err.into()),
    }
    let mut options = OpenOptions::new();
    options.read(true).write(true).create(true);
    #[cfg(unix)]
    {
        options.mode(0o600);
        // Best-effort O_NOFOLLOW when the platform defines it (macOS/Linux).
        #[cfg(target_os = "macos")]
        options.custom_flags(0x00000100); // O_NOFOLLOW
        #[cfg(target_os = "linux")]
        options.custom_flags(0x20000); // O_NOFOLLOW
    }
    let lockfile = options.open(&lock_path).map_err(|err| {
        QueueError::Store(format!(
            "failed to open lock file {}: {err}",
            lock_path.display()
        ))
    })?;
    let meta = lockfile.metadata()?;
    if !meta.is_file() {
        return Err(QueueError::Store(format!(
            "expected regular file, found non-regular: {}",
            lock_path.display()
        )));
    }
    #[cfg(unix)]
    lockfile.set_permissions(fs::Permissions::from_mode(0o600))?;
    lockfile.lock_exclusive()?;
    let result = f();
    let _ = lockfile.unlock();
    result
}

fn dispatch_dir(store_dir: &Path) -> Result<PathBuf, QueueError> {
    // Application queue leaf first, then dispatch child — never chmod parents above store_dir.
    crate::queue::ensure_store_dir(store_dir)?;
    let path = store_dir.join("dispatch");
    crate::queue::ensure_dir_mode(&path, 0o700)?;
    Ok(path)
}

fn bundle_path(dispatch_dir: &Path, work_id: &str) -> Result<PathBuf, QueueError> {
    let safe: String = work_id
        .chars()
        .filter(|ch| ch.is_alphanumeric() || *ch == '-' || *ch == '_')
        .collect();
    if safe.is_empty() {
        return Err(QueueError::Usage("work_id is empty".into()));
    }
    if safe != work_id {
        return Err(QueueError::Usage(format!("invalid work_id: {work_id}")));
    }
    crate::queue::ensure_dir_mode(dispatch_dir, 0o700)?;
    Ok(dispatch_dir.join(format!("{safe}.json")))
}

fn read_bundle(path: &Path) -> Result<Value, QueueError> {
    if path.is_symlink() {
        return Err(QueueError::Store(format!(
            "expected regular file, found symlink: {}",
            path.display()
        )));
    }
    if !path.exists() {
        return Err(QueueError::Store(format!(
            "dispatch bundle not found: {}",
            path.display()
        )));
    }
    crate::queue::ensure_regular_file_mode(path, 0o600)?;
    let raw = fs::read_to_string(path)?;
    let bundle: Value = serde_json::from_str(&raw)?;
    if !bundle.is_object() {
        return Err(QueueError::Store(format!(
            "dispatch bundle expected object, found {}: {}",
            value_kind(&bundle),
            path.display()
        )));
    }
    Ok(bundle)
}

fn steps_mut(bundle: &mut Value) -> Result<&mut Vec<Value>, QueueError> {
    let found = value_kind(bundle.get("steps").unwrap_or(&Value::Null));
    bundle
        .as_object_mut()
        .and_then(|obj| obj.get_mut("steps"))
        .and_then(Value::as_array_mut)
        .ok_or_else(|| {
            QueueError::Store(format!(
                "dispatch bundle expected steps array, found {}",
                found
            ))
        })
}

fn steps_ref(bundle: &Value) -> Result<&Vec<Value>, QueueError> {
    bundle
        .get("steps")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            QueueError::Store(format!(
                "dispatch bundle expected steps array, found {}",
                value_kind(bundle.get("steps").unwrap_or(&Value::Null))
            ))
        })
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

fn stale_running(step: &Value, now: f64) -> bool {
    if step.get("status").and_then(Value::as_str) != Some("running") {
        return false;
    }
    let updated_at = step
        .get("updated_at")
        .and_then(Value::as_f64)
        .unwrap_or(0.0);
    now - updated_at > RUNNING_LEASE_SECS
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
        crate::queue::ensure_dir_mode(parent, 0o700)?;
    }
    let serialized = serde_json::to_string_pretty(payload)?;
    let temp = temp_path_for(path);
    write_private_file(&temp, format!("{serialized}\n"))?;
    if let Err(err) = fs::rename(&temp, path) {
        let _ = fs::remove_file(&temp);
        return Err(err.into());
    }
    // Target inherits temp mode on rename; re-assert leaf mode without recursion.
    crate::queue::ensure_regular_file_mode(path, 0o600)?;
    Ok(())
}

fn write_private_file(path: &Path, contents: String) -> Result<(), QueueError> {
    // create_new: refuse to truncate a pre-existing temp symlink or file.
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    options.mode(0o600);
    let mut file = options.open(path)?;
    #[cfg(unix)]
    file.set_permissions(fs::Permissions::from_mode(0o600))?;
    use std::io::Write;
    file.write_all(contents.as_bytes())?;
    file.sync_all()?;
    Ok(())
}

fn temp_path_for(path: &Path) -> PathBuf {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    let name = path
        .file_name()
        .and_then(|part| part.to_str())
        .unwrap_or("bundle.json");
    path.with_file_name(format!(".{name}.{}.{}.tmp", std::process::id(), nanos))
}

fn value_kind(value: &Value) -> &'static str {
    match value {
        Value::Null => "missing",
        Value::Bool(_) => "bool",
        Value::Number(_) => "number",
        Value::String(_) => "string",
        Value::Array(_) => "array",
        Value::Object(_) => "object",
    }
}

fn now_secs() -> f64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or(0.0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_store() -> PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        std::env::temp_dir().join(format!("cluxion-dispatch-{}-{}", std::process::id(), nanos))
    }

    #[test]
    fn rejects_work_id_alias_vic_tim() {
        let store = temp_store();
        let victim = json!({
            "work_id": "victim",
            "steps": [{
                "step_id": "s1",
                "segment_id": "g1",
                "checksum": "c1",
                "token_estimate": 1,
                "content": "x",
                "status": "queued",
                "result": "",
                "error": ""
            }]
        });
        persist_bundle(&store, &json!({"work_id": "victim", "bundle": victim}))
            .expect("persist victim");
        let err = next_step(&store, &json!({"work_id": "vic!tim"})).unwrap_err();
        assert!(
            err.to_string().contains("invalid"),
            "expected invalid work_id, got {err}"
        );
        let claimed = next_step(&store, &json!({"work_id": "victim"})).expect("victim next");
        assert_eq!(claimed["ready"], true);
        let _ = fs::remove_dir_all(&store);
    }

    #[test]
    fn accepts_unicode_alphanumeric_work_id() {
        let store = temp_store();
        let work_id = "작업-테스트1";
        let bundle = json!({
            "work_id": work_id,
            "steps": [{
                "step_id": "s1",
                "segment_id": "g1",
                "checksum": "c1",
                "token_estimate": 1,
                "content": "x",
                "status": "queued",
                "result": "",
                "error": ""
            }]
        });
        persist_bundle(&store, &json!({"work_id": work_id, "bundle": bundle}))
            .expect("persist unicode");
        let claimed = next_step(&store, &json!({"work_id": work_id})).expect("next");
        assert_eq!(claimed["ready"], true);
        assert_eq!(claimed["work_id"], work_id);
        let _ = fs::remove_dir_all(&store);
    }

    #[cfg(unix)]
    #[test]
    fn dispatch_only_sets_store_and_dispatch_0700() {
        use std::os::unix::fs::PermissionsExt;
        let store = temp_store();
        let parent = store.parent().expect("parent").to_path_buf();
        let parent_mode = fs::metadata(&parent).unwrap().permissions().mode();
        let bundle = json!({
            "work_id": "d1",
            "steps": [{
                "step_id": "s1",
                "segment_id": "g1",
                "checksum": "c1",
                "token_estimate": 1,
                "content": "x",
                "status": "queued",
                "result": "",
                "error": ""
            }]
        });
        persist_bundle(&store, &json!({"work_id": "d1", "bundle": bundle})).expect("persist");
        let store_mode = fs::metadata(&store).unwrap().permissions().mode() & 0o777;
        let dispatch_mode = fs::metadata(store.join("dispatch"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(store_mode, 0o700);
        assert_eq!(dispatch_mode, 0o700);
        assert_eq!(
            fs::metadata(&parent).unwrap().permissions().mode(),
            parent_mode
        );
        let _ = fs::remove_dir_all(&store);
    }

    #[cfg(unix)]
    #[test]
    fn rejects_planted_bundle_and_lock_symlinks_without_touching_victims() {
        use std::os::unix::fs::PermissionsExt;
        let store = temp_store();
        fs::create_dir_all(store.join("dispatch")).unwrap();
        let victim = store.join("outside-victim.json");
        fs::write(&victim, b"SECRET").unwrap();
        fs::set_permissions(&victim, fs::Permissions::from_mode(0o644)).unwrap();
        let victim_mode = fs::metadata(&victim).unwrap().permissions().mode();
        let victim_bytes = fs::read(&victim).unwrap();

        let link = store.join("dispatch").join("alias.json");
        std::os::unix::fs::symlink(&victim, &link).unwrap();
        let err = next_step(&store, &json!({"work_id": "alias"})).unwrap_err();
        assert!(
            err.to_string().contains("symlink") || err.to_string().contains("expected"),
            "got {err}"
        );
        assert_eq!(fs::read(&victim).unwrap(), victim_bytes);
        assert_eq!(
            fs::metadata(&victim).unwrap().permissions().mode(),
            victim_mode
        );

        let lock_victim = store.join("lock-victim");
        fs::write(&lock_victim, b"LOCK").unwrap();
        fs::set_permissions(&lock_victim, fs::Permissions::from_mode(0o644)).unwrap();
        let lock_mode = fs::metadata(&lock_victim).unwrap().permissions().mode();
        let lock_bytes = fs::read(&lock_victim).unwrap();
        let lock_link = store.join("dispatch").join(".dispatch.lock");
        let _ = fs::remove_file(&lock_link);
        std::os::unix::fs::symlink(&lock_victim, &lock_link).unwrap();
        let bundle = json!({
            "work_id": "safe",
            "steps": [{
                "step_id": "s1",
                "segment_id": "g1",
                "checksum": "c1",
                "token_estimate": 1,
                "content": "x",
                "status": "queued",
                "result": "",
                "error": ""
            }]
        });
        let err =
            persist_bundle(&store, &json!({"work_id": "safe", "bundle": bundle})).unwrap_err();
        assert!(
            err.to_string().contains("symlink") || err.to_string().contains("expected"),
            "got {err}"
        );
        assert_eq!(fs::read(&lock_victim).unwrap(), lock_bytes);
        assert_eq!(
            fs::metadata(&lock_victim).unwrap().permissions().mode(),
            lock_mode
        );
        let _ = fs::remove_dir_all(&store);
    }
}
