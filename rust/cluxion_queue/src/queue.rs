use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

use rusqlite::{params, Connection};
use serde_json::{json, Value};

use crate::types::{ok_payload, require_str, QueueError};

// In-process connection cache: opening + schema-init per op dominates latency
// when this crate runs as an extension module. Keyed by store_dir; WAL keeps
// cross-process access safe.
static CONN_CACHE: OnceLock<Mutex<HashMap<PathBuf, Connection>>> = OnceLock::new();

fn with_db<T>(
    store_dir: &Path,
    op: impl FnOnce(&Connection) -> Result<T, QueueError>,
) -> Result<T, QueueError> {
    let cache = CONN_CACHE.get_or_init(|| Mutex::new(HashMap::new()));
    let mut guard = cache
        .lock()
        .map_err(|_| QueueError::Store("connection cache poisoned".into()))?;
    if !guard.contains_key(store_dir) {
        let conn = open_db(store_dir)?;
        guard.insert(store_dir.to_path_buf(), conn);
    }
    let conn = guard.get(store_dir).expect("connection just inserted");
    op(conn)
}

fn open_db(store_dir: &Path) -> Result<Connection, QueueError> {
    std::fs::create_dir_all(store_dir)?;
    let db_path = store_dir.join("work_queue.sqlite");
    let conn = Connection::open(db_path)?;
    conn.execute_batch(
        "PRAGMA journal_mode=WAL;
         PRAGMA synchronous=NORMAL;
         CREATE TABLE IF NOT EXISTS work_queue (
             work_id TEXT PRIMARY KEY,
             prompt TEXT NOT NULL,
             surface TEXT NOT NULL DEFAULT 'api',
             priority INTEGER NOT NULL DEFAULT 2,
             status TEXT NOT NULL DEFAULT 'pending',
             metadata_json TEXT NOT NULL DEFAULT '{}',
             sequence INTEGER NOT NULL,
             created_at REAL NOT NULL,
             updated_at REAL NOT NULL
         );
         CREATE INDEX IF NOT EXISTS idx_work_queue_status_priority
             ON work_queue(status, priority, sequence);",
    )?;
    Ok(conn)
}

pub fn enqueue(store_dir: &Path, payload: &Value) -> Result<Value, QueueError> {
    let work_id = require_str(payload, "work_id")?;
    let prompt = require_str(payload, "prompt")?;
    let surface = payload
        .get("surface")
        .and_then(Value::as_str)
        .unwrap_or("api");
    let priority = payload.get("priority").and_then(Value::as_i64).unwrap_or(2);
    let metadata_json = payload
        .get("metadata")
        .map(|value| serde_json::to_string(value).unwrap_or_else(|_| "{}".into()))
        .unwrap_or_else(|| "{}".into());
    let now = now_secs();
    with_db(store_dir, |conn| {
        let sequence: i64 = conn.query_row(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM work_queue",
            [],
            |row| row.get(0),
        )?;
        conn.execute(
        "INSERT INTO work_queue (work_id, prompt, surface, priority, status, metadata_json, sequence, created_at, updated_at)
         VALUES (?1, ?2, ?3, ?4, 'pending', ?5, ?6, ?7, ?7)
         ON CONFLICT(work_id) DO UPDATE SET
             prompt=excluded.prompt,
             surface=excluded.surface,
             priority=excluded.priority,
             metadata_json=excluded.metadata_json,
             status='pending',
             updated_at=excluded.updated_at",
        params![work_id, prompt, surface, priority, metadata_json, sequence, now],
    )?;
        Ok(ok_payload(json!({
            "accepted": true,
            "work_id": work_id,
            "sequence": sequence,
            "reason": "queued",
        })))
    })
}

pub fn dequeue(store_dir: &Path, _payload: &Value) -> Result<Value, QueueError> {
    let now = now_secs();
    with_db(store_dir, |conn| {
        // Use BEGIN IMMEDIATE for atomic SELECT-then-UPDATE claim
        conn.execute_batch("BEGIN IMMEDIATE;")?;
        let row = conn.query_row(
            "SELECT work_id, prompt, surface, priority, metadata_json
         FROM work_queue
         WHERE status = 'pending'
         ORDER BY priority ASC, sequence ASC
         LIMIT 1",
            [],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, i64>(3)?,
                    row.get::<_, String>(4)?,
                ))
            },
        );
        match row {
            Ok((work_id, prompt, surface, priority, metadata_json)) => {
                conn.execute(
                    "UPDATE work_queue SET status='running', updated_at=?2 WHERE work_id=?1",
                    params![work_id, now],
                )?;
                conn.execute_batch("COMMIT;")?;
                Ok(ok_payload(json!({
                    "ready": true,
                    "item": {
                        "work_id": work_id,
                        "prompt": prompt,
                        "surface": surface,
                        "priority": priority,
                        "metadata": serde_json::from_str::<Value>(&metadata_json).unwrap_or(json!({})),
                    }
                })))
            }
            Err(rusqlite::Error::QueryReturnedNoRows) => {
                conn.execute_batch("COMMIT;")?;
                Ok(ok_payload(json!({
                    "ready": false,
                    "item": Value::Null,
                })))
            }
            Err(err) => {
                let _ = conn.execute_batch("ROLLBACK;");
                Err(QueueError::Sqlite(err))
            }
        }
    })
}

pub fn peek(store_dir: &Path, payload: &Value) -> Result<Value, QueueError> {
    let limit = payload.get("limit").and_then(Value::as_u64).unwrap_or(16) as i64;
    with_db(store_dir, |conn| {
        let mut stmt = conn.prepare(
            "SELECT work_id, priority, status, sequence
         FROM work_queue
         ORDER BY priority ASC, sequence ASC
         LIMIT ?1",
        )?;
        let rows = stmt.query_map(params![limit], |row| {
            Ok(json!({
                "work_id": row.get::<_, String>(0)?,
                "priority": row.get::<_, i64>(1)?,
                "status": row.get::<_, String>(2)?,
                "sequence": row.get::<_, i64>(3)?,
            }))
        })?;
        let order: Vec<Value> = rows.filter_map(Result::ok).collect();
        Ok(ok_payload(json!({
            "order": order,
            "size": order.len(),
        })))
    })
}

pub fn status(store_dir: &Path, _payload: &Value) -> Result<Value, QueueError> {
    with_db(store_dir, |conn| {
        let pending: i64 = conn.query_row(
            "SELECT COUNT(*) FROM work_queue WHERE status='pending'",
            [],
            |row| row.get(0),
        )?;
        let running: i64 = conn.query_row(
            "SELECT COUNT(*) FROM work_queue WHERE status='running'",
            [],
            |row| row.get(0),
        )?;
        Ok(ok_payload(json!({
            "pending": pending,
            "running": running,
            "backend": "rust_sqlite",
        })))
    })
}

fn now_secs() -> f64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or(0.0)
}
