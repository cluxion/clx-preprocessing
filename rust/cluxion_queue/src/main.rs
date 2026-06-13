use std::env;
use std::io::{self, Read};
use std::process;

use serde_json::Value;

use cluxion_queue_native::run_command;
use cluxion_queue_native::types::QueueError;

fn main() {
    if let Err(err) = run() {
        let payload = serde_json::json!({
            "ok": false,
            "error": err.to_string(),
        });
        println!(
            "{}",
            serde_json::to_string(&payload).unwrap_or_else(|_| "{\"ok\":false}".into())
        );
        process::exit(1);
    }
}

fn run() -> Result<(), QueueError> {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        return Err(QueueError::Usage(
            "usage: cluxion-queue <enqueue|dequeue|peek|persist|next|record|brief|status|guard-sample|guard-scan|guard-daemon>".into(),
        ));
    }
    let command = args[1].as_str();
    // Long-running mode: poll and publish guard state until killed.
    // Reads its config from argv, not stdin, so it can be spawned detached.
    if command == "guard-daemon" {
        let store_dir = cluxion_queue_native::store_dir_from_payload(&serde_json::json!({
            "store_dir": args.get(2).cloned().unwrap_or_default(),
        }))?;
        let interval_ms = args
            .get(3)
            .and_then(|raw| raw.parse::<u64>().ok())
            .unwrap_or(cluxion_queue_native::guard::DEFAULT_DAEMON_INTERVAL_MS);
        let window = args
            .get(4)
            .and_then(|raw| raw.parse::<usize>().ok())
            .unwrap_or(cluxion_queue_native::guard::DEFAULT_DAEMON_WINDOW);
        return cluxion_queue_native::guard::run_daemon(&store_dir, interval_ms, window);
    }
    let payload = read_stdin_json()?;
    let result = run_command(command, &payload)?;
    println!("{}", serde_json::to_string(&result)?);
    Ok(())
}

fn read_stdin_json() -> Result<Value, QueueError> {
    let mut raw = String::new();
    io::stdin().read_to_string(&mut raw)?;
    if raw.trim().is_empty() {
        return Ok(Value::Object(serde_json::Map::new()));
    }
    Ok(serde_json::from_str(&raw)?)
}
