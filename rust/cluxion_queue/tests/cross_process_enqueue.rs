//! Cross-process enqueue race coverage. In-process threads cannot exercise
//! this: the CONN_CACHE mutex serializes them, so every enqueue shares one
//! connection. Here every enqueue is a real one-shot CLI process with its own
//! connection against a fresh shared store — the shape that lost enqueues to
//! "database is locked" and could collide on sequence before 0.3.41.

use std::io::Write;
use std::process::{Command, Stdio};

use rusqlite::Connection;

const PROCESSES: usize = 40;
const ENQUEUES_PER_PROCESS: usize = 2;
const ITERATIONS: usize = 5;

#[test]
fn cross_process_enqueue_is_lossless_and_duplicate_free() {
    let bin = env!("CARGO_BIN_EXE_cluxion-queue");
    let total = (PROCESSES * ENQUEUES_PER_PROCESS) as i64;

    for iteration in 0..ITERATIONS {
        let store =
            std::env::temp_dir().join(format!("cluxion-xproc-{}-{iteration}", std::process::id()));
        let _ = std::fs::remove_dir_all(&store);
        let store_str = store.to_string_lossy().to_string();

        // Spawn every enqueue before reaping any, so all processes contend on
        // the same fresh store (including the WAL switch + table creation).
        let mut children = Vec::with_capacity(PROCESSES * ENQUEUES_PER_PROCESS);
        for process in 0..PROCESSES {
            for slot in 0..ENQUEUES_PER_PROCESS {
                let payload = format!(
                    r#"{{"store_dir":"{store_str}","work_id":"w-{process}-{slot}","prompt":"x"}}"#
                );
                let mut child = Command::new(bin)
                    .arg("enqueue")
                    .stdin(Stdio::piped())
                    .stdout(Stdio::piped())
                    .spawn()
                    .expect("spawn cluxion-queue enqueue");
                child
                    .stdin
                    .take()
                    .expect("child stdin")
                    .write_all(payload.as_bytes())
                    .expect("write payload");
                children.push(child);
            }
        }

        let mut accepted = 0;
        let mut failures = Vec::new();
        for child in children {
            let output = child.wait_with_output().expect("child output");
            let stdout = String::from_utf8_lossy(&output.stdout);
            if output.status.success() && stdout.contains("\"accepted\":true") {
                accepted += 1;
            } else {
                failures.push(stdout.trim().to_string());
            }
        }
        assert_eq!(
            accepted, total,
            "iteration {iteration}: lost enqueues, failures: {failures:?}"
        );

        let conn = Connection::open(store.join("work_queue.sqlite")).expect("open store");
        let (rows, distinct, min_seq, max_seq) = conn
            .query_row(
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
            .expect("count query");
        assert_eq!(rows, total, "iteration {iteration}: rows lost");
        assert_eq!(
            distinct, total,
            "iteration {iteration}: duplicate sequences"
        );
        assert_eq!(
            min_seq, 1,
            "iteration {iteration}: sequence must start at 1"
        );
        assert_eq!(
            max_seq, total,
            "iteration {iteration}: sequence must be gapless"
        );

        let _ = std::fs::remove_dir_all(&store);
    }
}
