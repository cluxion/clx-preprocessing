use std::fs::{self, OpenOptions};
use std::path::{Path, PathBuf};
use std::time::Duration;

#[cfg(unix)]
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};

use rusqlite::{params, Connection, ErrorCode};
use serde_json::{json, Value};

use crate::types::{ok_payload, require_str, QueueError};

const BUSY_TIMEOUT: Duration = Duration::from_secs(30);
// Absorbs instant SQLITE_BUSY_SNAPSHOT rejections and unique-sequence
// collisions; genuine lock waits already block inside SQLite via BUSY_TIMEOUT.
const ENQUEUE_RETRY_ATTEMPTS: u32 = 5;
// The rollback->WAL switch only lasts until the first process wins it, but
// its EXCLUSIVE upgrade bypasses the busy handler (see open_db), so give the
// schema batch a longer runway: 20 attempts * 10ms*n backoff ~= 2.1s total.
const OPEN_RETRY_ATTEMPTS: u32 = 20;

const UNIQUE_SEQUENCE_INDEX: &str =
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_work_queue_sequence ON work_queue(sequence);";

fn with_db<T>(
    store_dir: &Path,
    op: impl FnOnce(&Connection) -> Result<T, QueueError>,
) -> Result<T, QueueError> {
    // A long-lived bundled-SQLite WAL connection can retain an orphaned view
    // after another runtime opens and closes the same DB. Per-command
    // connections keep native, subprocess, and Python state coherent.
    let conn = open_db(store_dir)?;
    op(&conn)
}

/// Tighten the application queue store leaf to 0700 (not parents above it).
pub(crate) fn ensure_store_dir(store_dir: &Path) -> Result<(), QueueError> {
    ensure_dir_mode(store_dir, 0o700)
}

pub(crate) fn ensure_dir_mode(path: &Path, mode: u32) -> Result<(), QueueError> {
    if path.is_symlink() {
        return Err(QueueError::Store(format!(
            "expected directory, found symlink: {}",
            path.display()
        )));
    }
    match fs::symlink_metadata(path) {
        Ok(meta) if meta.is_dir() => {}
        Ok(_) => {
            return Err(QueueError::Store(format!(
                "expected directory, found non-directory: {}",
                path.display()
            )));
        }
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            fs::create_dir_all(path)?;
        }
        Err(err) => return Err(err.into()),
    }
    #[cfg(unix)]
    {
        let meta = fs::symlink_metadata(path)?;
        if !meta.is_dir() {
            return Err(QueueError::Store(format!(
                "expected directory, found non-directory: {}",
                path.display()
            )));
        }
        if meta.file_type().is_symlink() {
            return Err(QueueError::Store(format!(
                "expected directory, found symlink: {}",
                path.display()
            )));
        }
        fs::set_permissions(path, fs::Permissions::from_mode(mode))?;
    }
    #[cfg(not(unix))]
    {
        let _ = mode;
    }
    Ok(())
}

pub(crate) fn ensure_regular_file_mode(path: &Path, mode: u32) -> Result<(), QueueError> {
    if path.is_symlink() {
        return Err(QueueError::Store(format!(
            "expected regular file, found symlink: {}",
            path.display()
        )));
    }
    match fs::symlink_metadata(path) {
        Ok(meta) if meta.is_file() => {
            #[cfg(unix)]
            fs::set_permissions(path, fs::Permissions::from_mode(mode))?;
            #[cfg(not(unix))]
            let _ = mode;
            Ok(())
        }
        Ok(_) => Err(QueueError::Store(format!(
            "expected regular file, found non-regular: {}",
            path.display()
        ))),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(err) => Err(err.into()),
    }
}

fn precreate_db_file(db_path: &Path) -> Result<(), QueueError> {
    if db_path.is_symlink() {
        return Err(QueueError::Store(format!(
            "expected regular file, found symlink: {}",
            db_path.display()
        )));
    }
    #[cfg(unix)]
    {
        if !db_path.exists() {
            let mut options = OpenOptions::new();
            options.write(true).create_new(true).mode(0o600);
            match options.open(db_path) {
                Ok(file) => {
                    file.set_permissions(fs::Permissions::from_mode(0o600))?;
                }
                // Concurrent creator won the race; tighten the existing regular file.
                Err(err) if err.kind() == std::io::ErrorKind::AlreadyExists => {
                    ensure_regular_file_mode(db_path, 0o600)?;
                }
                Err(err) => return Err(err.into()),
            }
        } else {
            ensure_regular_file_mode(db_path, 0o600)?;
        }
    }
    #[cfg(not(unix))]
    {
        let _ = db_path;
    }
    Ok(())
}

fn migrate_db_sidecars(db_path: &Path) -> Result<(), QueueError> {
    for suffix in ["-wal", "-shm"] {
        let side = PathBuf::from(format!("{}{suffix}", db_path.display()));
        ensure_regular_file_mode(&side, 0o600)?;
    }
    Ok(())
}

fn open_db(store_dir: &Path) -> Result<Connection, QueueError> {
    ensure_store_dir(store_dir)?;
    let db_path = store_dir.join("work_queue.sqlite");
    precreate_db_file(&db_path)?;
    migrate_db_sidecars(&db_path)?;
    let conn = Connection::open(&db_path)?;
    // busy_timeout must be installed before any other statement: on a fresh
    // store the journal-mode switch and table creation are write operations,
    // and concurrent one-shot processes racing them with the default timeout
    // (0) fail instantly with "database is locked" and lose the enqueue.
    conn.busy_timeout(BUSY_TIMEOUT)?;
    // busy_timeout alone cannot protect the rollback->WAL switch: PRAGMA
    // journal_mode=WAL upgrades an already-open read txn to EXCLUSIVE inside
    // sqlite3BtreeSetVersion, and that path returns SQLITE_BUSY without ever
    // consulting the busy handler. A fresh-store multi-process burst hits it
    // reliably, so retry the (idempotent) schema batch explicitly.
    let mut attempt = 0;
    loop {
        migrate_db_sidecars(&db_path)?;
        let schema = conn.execute_batch(
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
        );
        match schema {
            Ok(()) => break,
            Err(err) if attempt < OPEN_RETRY_ATTEMPTS && is_transient(&err) => {
                attempt += 1;
                std::thread::sleep(Duration::from_millis(10 * u64::from(attempt)));
            }
            Err(err) => return Err(QueueError::Sqlite(err)),
        }
    }
    migrate_db_sidecars(&db_path)?;
    ensure_unique_sequence_index(&conn);
    Ok(conn)
}

/// Sequence uniqueness backstop: with the index in place a racing writer that
/// somehow computed a stale MAX fails with SQLITE_CONSTRAINT instead of
/// silently committing a duplicate (enqueue retries with a fresh MAX).
/// Stores written by pre-0.3.41 builds can already hold duplicate sequences:
/// resequence them order-preserving and retry; if the index still cannot be
/// built, skip it — enqueue stays correct via BEGIN IMMEDIATE.
fn ensure_unique_sequence_index(conn: &Connection) {
    match conn.execute_batch(UNIQUE_SEQUENCE_INDEX) {
        Ok(()) => return,
        Err(err) if err.sqlite_error_code() == Some(ErrorCode::ConstraintViolation) => {}
        // Transient (e.g. lock contention): the next open_db retries.
        Err(_) => return,
    }
    // ponytail: O(n^2) correlated resequence — one-shot migration, queues are small.
    let migrated = conn.execute_batch(
        "BEGIN IMMEDIATE;
         UPDATE work_queue SET sequence = (
             SELECT COUNT(*) FROM work_queue AS w2
             WHERE w2.sequence < work_queue.sequence
                OR (w2.sequence = work_queue.sequence AND w2.rowid <= work_queue.rowid)
         );
         COMMIT;",
    );
    if migrated.is_err() {
        let _ = conn.execute_batch("ROLLBACK;");
        return;
    }
    let _ = conn.execute_batch(UNIQUE_SEQUENCE_INDEX);
}

const QUEUE_OWNER_KEYS: [&str; 3] = ["owner_cwd", "owner_session_id", "owner_scope"];

#[derive(Debug, Clone, PartialEq, Eq)]
struct QueueOwner {
    cwd: String,
    session_id: String,
    scope: String,
}

fn queue_owner_from_metadata(metadata: &Value) -> Result<Option<QueueOwner>, ()> {
    let obj = match metadata {
        Value::Null => return Ok(None),
        Value::Object(map) => map,
        _ => return Err(()),
    };
    let present = QUEUE_OWNER_KEYS.iter().any(|key| obj.contains_key(*key));
    if !present {
        return Ok(None);
    }
    if !QUEUE_OWNER_KEYS.iter().all(|key| obj.contains_key(*key)) {
        return Err(());
    }
    let cwd = match obj.get("owner_cwd") {
        Some(Value::String(s)) if !s.is_empty() => s.clone(),
        _ => return Err(()),
    };
    let session_id = match obj.get("owner_session_id") {
        Some(Value::String(s)) => s.clone(),
        _ => return Err(()),
    };
    let scope = match obj.get("owner_scope") {
        Some(Value::String(s)) if !s.is_empty() => s.clone(),
        _ => return Err(()),
    };
    Ok(Some(QueueOwner {
        cwd,
        session_id,
        scope,
    }))
}

fn queue_owners_conflict(existing: &Option<QueueOwner>, new: &Option<QueueOwner>) -> bool {
    match (existing, new) {
        (None, None) => false,
        (None, Some(_)) | (Some(_), None) => true,
        (Some(a), Some(b)) => a != b,
    }
}

enum EnqueueOutcome {
    Sequence(i64),
    Contract(Value),
}

pub fn enqueue(store_dir: &Path, payload: &Value) -> Result<Value, QueueError> {
    let work_id = require_str(payload, "work_id")?;
    let prompt = require_str(payload, "prompt")?;
    let surface = payload
        .get("surface")
        .and_then(Value::as_str)
        .unwrap_or("api");
    let priority = payload.get("priority").and_then(Value::as_i64).unwrap_or(2);
    let metadata = payload
        .get("metadata")
        .cloned()
        .unwrap_or_else(|| json!({}));
    let new_owner = match queue_owner_from_metadata(&metadata) {
        Ok(owner) => owner,
        Err(()) => {
            return Ok(json!({
                "ok": false,
                "accepted": false,
                "error": "invalid_queue_owner",
                "work_id": work_id,
            }));
        }
    };
    let metadata_json = serde_json::to_string(&metadata).unwrap_or_else(|_| "{}".into());
    let now = now_secs();
    with_db(store_dir, |conn| {
        // BEGIN IMMEDIATE so the MAX(sequence) read and the INSERT are atomic
        // across processes (matches py_queue._enqueue). The retry loop absorbs
        // transient SQLITE_BUSY (a burst must not lose enqueues) and
        // unique-sequence collisions (recompute MAX and try again).
        let mut last_err = None;
        for attempt in 0..ENQUEUE_RETRY_ATTEMPTS {
            if attempt > 0 {
                std::thread::sleep(Duration::from_millis(10 * u64::from(attempt)));
            }
            match enqueue_txn(
                conn,
                work_id,
                prompt,
                surface,
                priority,
                &metadata_json,
                &new_owner,
                now,
            ) {
                Ok(EnqueueOutcome::Sequence(sequence)) => {
                    return Ok(ok_payload(json!({
                        "accepted": true,
                        "work_id": work_id,
                        "sequence": sequence,
                        "reason": "queued",
                    })))
                }
                Ok(EnqueueOutcome::Contract(value)) => return Ok(value),
                Err(err) if is_transient(&err) => last_err = Some(err),
                Err(err) => return Err(QueueError::Sqlite(err)),
            }
        }
        Err(QueueError::Sqlite(
            last_err.expect("retry loop always records an error"),
        ))
    })
}

#[allow(clippy::too_many_arguments)]
fn enqueue_txn(
    conn: &Connection,
    work_id: &str,
    prompt: &str,
    surface: &str,
    priority: i64,
    metadata_json: &str,
    new_owner: &Option<QueueOwner>,
    now: f64,
) -> Result<EnqueueOutcome, rusqlite::Error> {
    conn.execute_batch("BEGIN IMMEDIATE;")?;
    let outcome = (|| {
        match conn.query_row(
            "SELECT metadata_json FROM work_queue WHERE work_id = ?1",
            params![work_id],
            |row| row.get::<_, String>(0),
        ) {
            Ok(existing_meta_json) => {
                // Decode failure or non-object must not downgrade to {}.
                let existing_meta: Value = match serde_json::from_str(&existing_meta_json) {
                    Ok(value) => value,
                    Err(_) => {
                        return Ok(EnqueueOutcome::Contract(json!({
                            "ok": false,
                            "accepted": false,
                            "error": "invalid_queue_owner",
                            "work_id": work_id,
                        })));
                    }
                };
                let existing_owner = match queue_owner_from_metadata(&existing_meta) {
                    Ok(owner) => owner,
                    Err(()) => {
                        return Ok(EnqueueOutcome::Contract(json!({
                            "ok": false,
                            "accepted": false,
                            "error": "invalid_queue_owner",
                            "work_id": work_id,
                        })));
                    }
                };
                if queue_owners_conflict(&existing_owner, new_owner) {
                    return Ok(EnqueueOutcome::Contract(json!({
                        "ok": false,
                        "accepted": false,
                        "error": "queue_owner_conflict",
                        "work_id": work_id,
                    })));
                }
            }
            Err(rusqlite::Error::QueryReturnedNoRows) => {}
            Err(err) => return Err(err),
        }
        // Equal owners (including both ownerless): legacy requeue path.
        // Intentional: resets running->pending even for identical payload.
        let sequence = conn.query_row(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM work_queue",
            [],
            |row| row.get::<_, i64>(0),
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
        // ON CONFLICT preserves sequence/created_at; return the stored
        // admission identity rather than the provisional MAX+1 candidate.
        let sequence = conn.query_row(
            "SELECT sequence FROM work_queue WHERE work_id = ?1",
            params![work_id],
            |row| row.get::<_, i64>(0),
        )?;
        Ok(EnqueueOutcome::Sequence(sequence))
    })();
    match &outcome {
        Ok(EnqueueOutcome::Sequence(_)) => {
            conn.execute_batch("COMMIT;")?;
        }
        Ok(EnqueueOutcome::Contract(_)) | Err(_) => {
            let _ = conn.execute_batch("ROLLBACK;");
        }
    }
    outcome
}

fn is_transient(err: &rusqlite::Error) -> bool {
    matches!(
        err.sqlite_error_code(),
        Some(ErrorCode::DatabaseBusy | ErrorCode::DatabaseLocked | ErrorCode::ConstraintViolation)
    )
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn concurrent_enqueue_assigns_unique_monotonic_sequences() {
        let store = std::env::temp_dir().join(format!(
            "cluxion-queue-enqueue-race-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let _ = std::fs::remove_dir_all(&store);

        let threads = 8;
        let per_thread = 25;
        let handles: Vec<_> = (0..threads)
            .map(|t| {
                let store = store.clone();
                std::thread::spawn(move || {
                    for i in 0..per_thread {
                        let payload = json!({
                            "work_id": format!("w-{t}-{i}"),
                            "prompt": "x",
                        });
                        enqueue(&store, &payload).expect("enqueue must succeed");
                    }
                })
            })
            .collect();
        for handle in handles {
            handle.join().expect("enqueue thread panicked");
        }

        let total = (threads * per_thread) as i64;
        let (rows, distinct, min_seq, max_seq) = with_db(&store, |conn| {
            conn.query_row(
                "SELECT COUNT(*), COUNT(DISTINCT sequence), MIN(sequence), MAX(sequence) FROM work_queue",
                [],
                |row| {
                    Ok((
                        row.get::<_, i64>(0)?,
                        row.get::<_, i64>(1)?,
                        row.get::<_, i64>(2)?,
                        row.get::<_, i64>(3)?,
                    ))
                },
            )
            .map_err(QueueError::Sqlite)
        })
        .expect("count query");
        assert_eq!(rows, total);
        assert_eq!(distinct, total, "duplicate sequence values assigned");
        assert_eq!(min_seq, 1);
        assert_eq!(max_seq, total);

        // Deterministic FIFO: first dequeue must claim sequence 1.
        let first = dequeue(&store, &json!({})).expect("dequeue");
        let claimed = first["item"]["work_id"].as_str().unwrap().to_string();
        let seq: i64 = with_db(&store, |conn| {
            conn.query_row(
                "SELECT sequence FROM work_queue WHERE work_id = ?1",
                params![claimed],
                |row| row.get(0),
            )
            .map_err(QueueError::Sqlite)
        })
        .expect("sequence lookup");
        assert_eq!(seq, 1);

        let _ = std::fs::remove_dir_all(&store);
    }

    #[test]
    fn reenqueue_same_work_id_returns_original_sequence() {
        let store = std::env::temp_dir().join(format!(
            "cluxion-queue-reenqueue-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let _ = std::fs::remove_dir_all(&store);

        let a1 = enqueue(&store, &json!({"work_id": "a", "prompt": "first-a"})).expect("enqueue a");
        assert_eq!(a1["sequence"], 1);
        let b1 = enqueue(&store, &json!({"work_id": "b", "prompt": "first-b"})).expect("enqueue b");
        assert_eq!(b1["sequence"], 2);

        let a2 = enqueue(&store, &json!({"work_id": "a", "prompt": "re-a"})).expect("re-enqueue a");
        assert_eq!(a2["ok"], true);
        assert_eq!(a2["accepted"], true);
        assert_eq!(
            a2["sequence"], 1,
            "duplicate work_id must return original admission sequence, not MAX+1"
        );
        assert!(a2.get("stored").is_none());
        assert!(a2.get("idempotent").is_none());
        assert!(a2.get("requeued").is_none());

        let peek = peek(&store, &json!({"limit": 16})).expect("peek");
        let order = peek["order"].as_array().expect("order array");
        assert_eq!(order.len(), 2);
        assert_eq!(order[0]["work_id"], "a");
        assert_eq!(order[0]["sequence"], 1);
        assert_eq!(order[1]["work_id"], "b");
        assert_eq!(order[1]["sequence"], 2);

        let d1 = dequeue(&store, &json!({})).expect("dequeue 1");
        assert_eq!(d1["item"]["work_id"], "a");
        let d2 = dequeue(&store, &json!({})).expect("dequeue 2");
        assert_eq!(d2["item"]["work_id"], "b");

        let _ = std::fs::remove_dir_all(&store);
    }

    fn owner_meta(cwd: &str, scope: &str) -> Value {
        json!({
            "owner_cwd": cwd,
            "owner_session_id": "",
            "owner_scope": scope,
        })
    }

    fn row_snapshot(
        store: &Path,
        work_id: &str,
    ) -> (String, String, i64, String, String, i64, f64, f64) {
        with_db(store, |conn| {
            conn.query_row(
                "SELECT prompt, surface, priority, status, metadata_json, sequence, created_at, updated_at
                 FROM work_queue WHERE work_id = ?1",
                params![work_id],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                        row.get(5)?,
                        row.get(6)?,
                        row.get(7)?,
                    ))
                },
            )
            .map_err(QueueError::Sqlite)
        })
        .expect("row snapshot")
    }

    #[test]
    fn queue_cross_owner_is_typed_conflict() {
        let store = std::env::temp_dir().join(format!(
            "cluxion-queue-owner-cross-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let _ = std::fs::remove_dir_all(&store);
        enqueue(
            &store,
            &json!({"work_id": "q1", "prompt": "a", "metadata": owner_meta("/tmp/a", "project:/tmp/a")}),
        )
        .expect("first");
        let before = row_snapshot(&store, "q1");
        let conflict = enqueue(
            &store,
            &json!({"work_id": "q1", "prompt": "b", "metadata": owner_meta("/tmp/b", "project:/tmp/b")}),
        )
        .expect("conflict contract");
        assert_eq!(conflict["ok"], false);
        assert_eq!(conflict["accepted"], false);
        assert_eq!(conflict["error"], "queue_owner_conflict");
        assert_eq!(conflict["work_id"], "q1");
        assert_eq!(row_snapshot(&store, "q1"), before);
        let _ = std::fs::remove_dir_all(&store);
    }

    #[test]
    fn queue_one_ownerless_both_directions_is_conflict() {
        let store = std::env::temp_dir().join(format!(
            "cluxion-queue-owner-one-side-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let _ = std::fs::remove_dir_all(&store);

        enqueue(
            &store,
            &json!({"work_id": "owned", "prompt": "a", "metadata": owner_meta("/tmp/a", "project:/tmp/a")}),
        )
        .expect("owned first");
        let before = row_snapshot(&store, "owned");
        let conflict = enqueue(&store, &json!({"work_id": "owned", "prompt": "ownerless"}))
            .expect("ownerless second");
        assert_eq!(conflict["error"], "queue_owner_conflict");
        assert_eq!(row_snapshot(&store, "owned"), before);

        enqueue(&store, &json!({"work_id": "bare", "prompt": "v1"})).expect("ownerless first");
        let before_b = row_snapshot(&store, "bare");
        let conflict_b = enqueue(
            &store,
            &json!({"work_id": "bare", "prompt": "v2", "metadata": owner_meta("/tmp/a", "project:/tmp/a")}),
        )
        .expect("owned second");
        assert_eq!(conflict_b["error"], "queue_owner_conflict");
        assert_eq!(row_snapshot(&store, "bare"), before_b);
        let _ = std::fs::remove_dir_all(&store);
    }

    #[test]
    fn queue_same_owner_legacy_requeue_resets_running() {
        let store = std::env::temp_dir().join(format!(
            "cluxion-queue-owner-requeue-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let _ = std::fs::remove_dir_all(&store);
        let payload = json!({
            "work_id": "q-same",
            "prompt": "same",
            "surface": "hermes",
            "priority": 1,
            "metadata": owner_meta("/tmp/a", "project:/tmp/a"),
        });
        let first = enqueue(&store, &payload).expect("first");
        dequeue(&store, &json!({})).expect("claim");
        let before = row_snapshot(&store, "q-same");
        assert_eq!(before.3, "running");
        let again = enqueue(&store, &payload).expect("again");
        assert_eq!(again["ok"], true);
        assert_eq!(again["accepted"], true);
        assert_eq!(again["sequence"], first["sequence"]);
        assert!(again.get("stored").is_none());
        assert!(again.get("idempotent").is_none());
        assert!(again.get("requeued").is_none());
        let after = row_snapshot(&store, "q-same");
        assert_eq!(after.3, "pending");
        assert_eq!(after.5, before.5);
        assert_eq!(after.6, before.6);
        let _ = std::fs::remove_dir_all(&store);
    }

    #[test]
    fn queue_both_ownerless_legacy_requeue() {
        let store = std::env::temp_dir().join(format!(
            "cluxion-queue-ownerless-requeue-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let _ = std::fs::remove_dir_all(&store);
        enqueue(&store, &json!({"work_id": "ol", "prompt": "v1"})).expect("first");
        let before = row_snapshot(&store, "ol");
        let again = enqueue(&store, &json!({"work_id": "ol", "prompt": "v2"})).expect("requeue");
        assert_eq!(again["ok"], true);
        assert_eq!(again["accepted"], true);
        assert_eq!(again["sequence"], before.5);
        let after = row_snapshot(&store, "ol");
        assert_eq!(after.0, "v2");
        assert_eq!(after.3, "pending");
        assert_eq!(after.5, before.5);
        assert_eq!(after.6, before.6);
        let _ = std::fs::remove_dir_all(&store);
    }

    #[test]
    fn queue_partial_owner_is_invalid_no_mutation() {
        let store = std::env::temp_dir().join(format!(
            "cluxion-queue-owner-invalid-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let _ = std::fs::remove_dir_all(&store);
        let result = enqueue(
            &store,
            &json!({
                "work_id": "bad",
                "prompt": "x",
                "metadata": {"owner_cwd": "/tmp/a", "owner_scope": "project:/tmp/a"},
            }),
        )
        .expect("invalid contract");
        assert_eq!(result["ok"], false);
        assert_eq!(result["accepted"], false);
        assert_eq!(result["error"], "invalid_queue_owner");
        assert_eq!(result["work_id"], "bad");

        enqueue(
            &store,
            &json!({"work_id": "keep", "prompt": "kept", "metadata": owner_meta("/tmp/a", "project:/tmp/a")}),
        )
        .expect("seed");
        let before = row_snapshot(&store, "keep");
        let bad = enqueue(
            &store,
            &json!({
                "work_id": "keep",
                "prompt": "mutate?",
                "metadata": {"owner_cwd": "/tmp/a", "owner_scope": "project:/tmp/a"},
            }),
        )
        .expect("malformed on existing");
        assert_eq!(bad["error"], "invalid_queue_owner");
        assert_eq!(row_snapshot(&store, "keep"), before);
        let _ = std::fs::remove_dir_all(&store);
    }

    #[test]
    fn queue_existing_invalid_metadata_json_is_typed_invalid() {
        let store = std::env::temp_dir().join(format!(
            "cluxion-queue-owner-corrupt-meta-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let _ = std::fs::remove_dir_all(&store);
        for seed in ["not-json{{{", "[1,2,3]", r#"{"owner_cwd":"/tmp/a"}"#] {
            let work_id = format!("corrupt-{}", seed.len());
            enqueue(&store, &json!({"work_id": work_id, "prompt": "kept"})).expect("seed");
            with_db(&store, |conn| {
                conn.execute(
                    "UPDATE work_queue SET metadata_json = ?1 WHERE work_id = ?2",
                    params![seed, work_id],
                )
                .map_err(QueueError::Sqlite)?;
                Ok(())
            })
            .expect("seed corrupt metadata");
            let before = row_snapshot(&store, &work_id);
            assert_eq!(before.4, seed);
            let result = enqueue(
                &store,
                &json!({
                    "work_id": work_id,
                    "prompt": "mutate?",
                    "metadata": owner_meta("/tmp/a", "project:/tmp/a"),
                }),
            )
            .expect("contract");
            assert_eq!(result["ok"], false);
            assert_eq!(result["accepted"], false);
            assert_eq!(result["error"], "invalid_queue_owner");
            assert_eq!(result["work_id"], work_id);
            assert_eq!(row_snapshot(&store, &work_id), before);
        }
        let _ = std::fs::remove_dir_all(&store);
    }

    #[test]
    fn queue_metadata_null_is_ownerless_null_serialization() {
        let store = std::env::temp_dir().join(format!(
            "cluxion-queue-meta-null-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let _ = std::fs::remove_dir_all(&store);
        let result = enqueue(
            &store,
            &json!({"work_id": "null-meta", "prompt": "x", "metadata": Value::Null}),
        )
        .expect("enqueue null metadata");
        assert_eq!(result["ok"], true);
        assert_eq!(result["accepted"], true);
        let row = row_snapshot(&store, "null-meta");
        assert_eq!(row.4, "null");
        let deq = dequeue(&store, &json!({})).expect("dequeue");
        assert_eq!(deq["ready"], true);
        assert!(deq["item"]["metadata"].is_null());
        let _ = std::fs::remove_dir_all(&store);
    }
}
