//! Deterministic context compression: stage 1 of the 70% -> 30% pipeline.
//!
//! Semantics-identical to the Python fallback in
//! `cluxion_runtime.core.context_compress` — every constant, threshold,
//! ordering rule, and the token estimator must stay in lockstep so the
//! three backends produce byte-identical output (parity-tested).
//!
//! What stays untouched: pinned messages (explicit `pinned`, the first
//! user message = task intent, the most recent `keep_recent` turns).
//! Stages run oldest-first and stop as soon as usage reaches the target:
//!   A. truncate long messages (head + tail excerpt)
//!   B. drop exact duplicates (trimmed-content match)
//!   C. fold remaining old turns into one-line digests
//! If the target is still not met the result carries `ai_summary_request`
//! telling the host AI which messages to summarize and what to preserve.

use serde_json::{json, Value};

use crate::types::QueueError;

const DEFAULT_CONTEXT_LIMIT: u64 = 128_000;
const DEFAULT_TRIGGER_RATIO: f64 = 0.70;
const DEFAULT_TARGET_RATIO: f64 = 0.30;
const DEFAULT_KEEP_RECENT: usize = 4;
const TRUNCATE_MIN_TOKENS: u64 = 512;
const TRUNCATE_HEAD_CHARS: usize = 1200;
const TRUNCATE_TAIL_CHARS: usize = 600;
const DEDUP_MIN_CHARS: usize = 40;
const DIGEST_LINE_CHARS: usize = 120;
const SUMMARY_REQUEST_LIMIT: usize = 8;

/// Known context windows by model-name substring, checked in order.
/// Conservative: only widely fixed values; everything else falls back to
/// DEFAULT_CONTEXT_LIMIT (callers should pass context_limit_tokens).
const MODEL_CONTEXT: [(&str, u64); 4] = [
    ("claude", 200_000),
    ("gemini", 1_000_000),
    ("gpt", 128_000),
    ("llama", 128_000),
];

struct Msg {
    role: String,
    content: String,
    pinned: bool,
}

pub fn compress(payload: &Value) -> Result<Value, QueueError> {
    let raw_messages = match payload.get("messages") {
        None => return Err(QueueError::Usage("missing required field: messages".into())),
        Some(value) => value
            .as_array()
            .ok_or_else(|| QueueError::Usage("messages must be a list".into()))?,
    };
    let mut messages: Vec<Msg> = Vec::with_capacity(raw_messages.len());
    for raw in raw_messages {
        messages.push(Msg {
            role: raw
                .get("role")
                .and_then(Value::as_str)
                .unwrap_or("user")
                .to_string(),
            content: raw
                .get("content")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            pinned: raw.get("pinned").and_then(Value::as_bool).unwrap_or(false),
        });
    }

    let context_limit = resolve_context_limit(payload);
    let trigger_ratio = ratio(payload, "trigger_ratio", DEFAULT_TRIGGER_RATIO);
    let target_ratio = ratio(payload, "target_ratio", DEFAULT_TARGET_RATIO);
    let keep_recent = payload
        .get("keep_recent_turns")
        .and_then(Value::as_u64)
        .map(|v| v as usize)
        .unwrap_or(DEFAULT_KEEP_RECENT);

    let tokens_before: u64 = messages.iter().map(|m| estimate_tokens(&m.content)).sum();
    let usage_before = tokens_before as f64 / context_limit as f64;
    let target_tokens = (target_ratio * context_limit as f64) as u64;

    if usage_before < trigger_ratio {
        return Ok(result_payload(
            &messages,
            tokens_before,
            tokens_before,
            context_limit,
            &[],
            None,
            &pinned_indices(&messages, keep_recent),
        ));
    }

    let pinned = pinned_indices(&messages, keep_recent);
    let mut stages: Vec<&str> = Vec::new();

    let mut total = tokens_before;
    if stage_truncate(&mut messages, &pinned, &mut total, target_tokens) {
        stages.push("truncate");
    }
    if total > target_tokens && stage_dedup(&mut messages, &pinned, &mut total, target_tokens) {
        stages.push("dedup");
    }
    if total > target_tokens && stage_digest(&mut messages, &pinned, &mut total, target_tokens) {
        stages.push("digest");
    }

    let summary_request = if total > target_tokens {
        Some(build_summary_request(
            &messages,
            &pinned,
            total,
            target_tokens,
        ))
    } else {
        None
    };

    Ok(result_payload(
        &messages,
        tokens_before,
        total,
        context_limit,
        &stages,
        summary_request,
        &pinned,
    ))
}

/// Mirror of `cluxion_runtime.core.preprocess.estimate_tokens`.
pub fn estimate_tokens(text: &str) -> u64 {
    let mut non_ascii: u64 = 0;
    let mut ascii: u64 = 0;
    for ch in text.chars() {
        if (ch as u32) > 127 {
            non_ascii += 1;
        } else {
            ascii += 1;
        }
    }
    std::cmp::max(1, non_ascii + ascii / 4)
}

fn resolve_context_limit(payload: &Value) -> u64 {
    if let Some(limit) = payload.get("context_limit_tokens").and_then(Value::as_u64) {
        if limit > 0 {
            return limit;
        }
    }
    if let Some(model) = payload.get("model").and_then(Value::as_str) {
        let lowered = model.to_lowercase();
        for (pattern, limit) in MODEL_CONTEXT {
            if lowered.contains(pattern) {
                return limit;
            }
        }
    }
    DEFAULT_CONTEXT_LIMIT
}

fn ratio(payload: &Value, key: &str, default: f64) -> f64 {
    match payload.get(key).and_then(Value::as_f64) {
        Some(v) if v > 0.0 && v < 1.0 => v,
        _ => default,
    }
}

fn pinned_indices(messages: &[Msg], keep_recent: usize) -> Vec<usize> {
    let mut pinned: Vec<usize> = Vec::new();
    for (idx, msg) in messages.iter().enumerate() {
        if msg.pinned {
            pinned.push(idx);
        }
    }
    if let Some(first_user) = messages.iter().position(|m| m.role == "user") {
        if !pinned.contains(&first_user) {
            pinned.push(first_user);
        }
    }
    let recent_start = messages.len().saturating_sub(keep_recent);
    for idx in recent_start..messages.len() {
        if !pinned.contains(&idx) {
            pinned.push(idx);
        }
    }
    pinned.sort_unstable();
    pinned
}

fn stage_truncate(messages: &mut [Msg], pinned: &[usize], total: &mut u64, target: u64) -> bool {
    let mut changed = false;
    for idx in 0..messages.len() {
        if *total <= target {
            break;
        }
        if pinned.contains(&idx) {
            continue;
        }
        let tokens = estimate_tokens(&messages[idx].content);
        if tokens <= TRUNCATE_MIN_TOKENS {
            continue;
        }
        let chars: Vec<char> = messages[idx].content.chars().collect();
        if chars.len() <= TRUNCATE_HEAD_CHARS + TRUNCATE_TAIL_CHARS {
            continue;
        }
        let elided = chars.len() - TRUNCATE_HEAD_CHARS - TRUNCATE_TAIL_CHARS;
        let head: String = chars[..TRUNCATE_HEAD_CHARS].iter().collect();
        let tail: String = chars[chars.len() - TRUNCATE_TAIL_CHARS..].iter().collect();
        let replacement = format!("{head}\n[...cluxion: {elided} chars elided...]\n{tail}");
        *total = *total - tokens + estimate_tokens(&replacement);
        messages[idx].content = replacement;
        changed = true;
    }
    changed
}

fn stage_dedup(messages: &mut [Msg], pinned: &[usize], total: &mut u64, target: u64) -> bool {
    let mut changed = false;
    let mut seen: std::collections::HashMap<String, usize> = std::collections::HashMap::new();
    for idx in 0..messages.len() {
        let trimmed = messages[idx].content.trim().to_string();
        if trimmed.chars().count() < DEDUP_MIN_CHARS {
            continue;
        }
        if let Some(first) = seen.get(&trimmed).copied() {
            if *total <= target || pinned.contains(&idx) {
                continue;
            }
            let tokens = estimate_tokens(&messages[idx].content);
            let replacement = format!("[cluxion: duplicate of message #{first} elided]");
            *total = *total - tokens + estimate_tokens(&replacement);
            messages[idx].content = replacement;
            changed = true;
        } else {
            seen.insert(trimmed, idx);
        }
    }
    changed
}

fn stage_digest(messages: &mut [Msg], pinned: &[usize], total: &mut u64, target: u64) -> bool {
    let mut changed = false;
    for idx in 0..messages.len() {
        if *total <= target {
            break;
        }
        if pinned.contains(&idx) {
            continue;
        }
        let tokens = estimate_tokens(&messages[idx].content);
        let first_line: String = messages[idx]
            .content
            .split('\n')
            .next()
            .unwrap_or("")
            .chars()
            .take(DIGEST_LINE_CHARS)
            .collect();
        let replacement = format!(
            "[cluxion digest] {}: {first_line} [{tokens} tokens elided]",
            messages[idx].role
        );
        let new_tokens = estimate_tokens(&replacement);
        if new_tokens >= tokens {
            continue;
        }
        *total = *total - tokens + new_tokens;
        messages[idx].content = replacement;
        changed = true;
    }
    changed
}

fn build_summary_request(messages: &[Msg], pinned: &[usize], total: u64, target: u64) -> Value {
    let mut candidates: Vec<(u64, usize)> = messages
        .iter()
        .enumerate()
        .filter(|(idx, _)| !pinned.contains(idx))
        .map(|(idx, m)| (estimate_tokens(&m.content), idx))
        .collect();
    candidates.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));
    let indices: Vec<usize> = candidates
        .into_iter()
        .take(SUMMARY_REQUEST_LIMIT)
        .map(|(_, idx)| idx)
        .collect();
    json!({
        "reason": "deterministic stages insufficient",
        "current_tokens": total,
        "target_tokens": target,
        "summarize_indices": indices,
        "instructions": "Summarize each listed message, preserving: user intent, decisions made, unresolved items, file paths and identifiers. Replace each with a summary under 10% of its original length.",
    })
}

fn result_payload(
    messages: &[Msg],
    tokens_before: u64,
    tokens_after: u64,
    context_limit: u64,
    stages: &[&str],
    summary_request: Option<Value>,
    pinned: &[usize],
) -> Value {
    let rendered: Vec<Value> = messages
        .iter()
        .map(|m| json!({"role": m.role, "content": m.content, "pinned": m.pinned}))
        .collect();
    json!({
        "ok": true,
        "compressed": !stages.is_empty(),
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "usage_before": tokens_before as f64 / context_limit as f64,
        "usage_after": tokens_after as f64 / context_limit as f64,
        "context_limit": context_limit,
        "stages_applied": stages,
        "pinned_indices": pinned,
        "messages": rendered,
        "ai_summary_request": summary_request,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn long_text(chars: usize) -> String {
        "x".repeat(chars)
    }

    #[test]
    fn token_estimator_matches_python_semantics() {
        assert_eq!(estimate_tokens(""), 1);
        assert_eq!(estimate_tokens("abcd"), 1);
        assert_eq!(estimate_tokens("한글텍스트"), 5);
        assert_eq!(estimate_tokens("ab한"), 1); // 1 cjk + 2 ascii / 4 = 1
    }

    #[test]
    fn below_trigger_is_noop() {
        let payload = json!({
            "messages": [{"role": "user", "content": "hello"}],
            "context_limit_tokens": 1000,
        });
        let result = compress(&payload).expect("compress");
        assert_eq!(result["compressed"], false);
        assert_eq!(result["stages_applied"].as_array().unwrap().len(), 0);
    }

    #[test]
    fn compresses_to_target_and_keeps_pinned() {
        let big = long_text(4000);
        let payload = json!({
            "messages": [
                {"role": "user", "content": "the task intent"},
                {"role": "assistant", "content": big},
                {"role": "tool", "content": big},
                {"role": "assistant", "content": big},
                {"role": "user", "content": "recent question"},
            ],
            "context_limit_tokens": 3000,
            "keep_recent_turns": 1,
        });
        let result = compress(&payload).expect("compress");
        assert_eq!(result["compressed"], true);
        let after = result["tokens_after"].as_u64().unwrap();
        let before = result["tokens_before"].as_u64().unwrap();
        assert!(after < before);
        // intent + most recent messages untouched
        let messages = result["messages"].as_array().unwrap();
        assert_eq!(messages[0]["content"], "the task intent");
        assert_eq!(messages[4]["content"], "recent question");
    }

    #[test]
    fn impossible_target_requests_ai_summary() {
        let payload = json!({
            "messages": [
                {"role": "user", "content": long_text(3000)},
                {"role": "assistant", "content": long_text(3000)},
            ],
            "context_limit_tokens": 1000,
            "keep_recent_turns": 2,
        });
        // everything pinned (first user + 2 recent) -> nothing compressible
        let result = compress(&payload).expect("compress");
        assert!(result["ai_summary_request"].is_object());
    }
}
