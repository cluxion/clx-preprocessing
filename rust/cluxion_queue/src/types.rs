use serde::{Deserialize, Serialize};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum QueueError {
    #[error("{0}")]
    Usage(String),
    #[error("queue store error: {0}")]
    Store(String),
    #[error("json error: {0}")]
    Json(#[from] serde_json::Error),
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("sqlite error: {0}")]
    Sqlite(#[from] rusqlite::Error),
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WorkQueueItem {
    pub work_id: String,
    pub prompt: String,
    pub surface: String,
    pub priority: i64,
    pub status: String,
    pub metadata_json: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DispatchStep {
    pub step_id: String,
    pub segment_id: String,
    pub checksum: String,
    pub token_estimate: i64,
    pub content: String,
    pub status: String,
    pub result: String,
    pub error: String,
}

pub fn ok_payload(data: serde_json::Value) -> serde_json::Value {
    let mut map = serde_json::Map::new();
    map.insert("ok".into(), true.into());
    if let serde_json::Value::Object(inner) = data {
        for (key, value) in inner {
            map.insert(key, value);
        }
    }
    serde_json::Value::Object(map)
}

pub fn require_str<'a>(payload: &'a serde_json::Value, key: &str) -> Result<&'a str, QueueError> {
    payload
        .get(key)
        .and_then(serde_json::Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| QueueError::Usage(format!("missing required field: {key}")))
}
