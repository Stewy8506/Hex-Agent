//! Shared wire types for the E2EE P2P data plane between HEX devices.
//!
//! `TurnCommitBundle` replicates a completed work turn from the Team Primary
//! Host to Replica Peers; its digest is the cross-language canonical SHA-256
//! shared with the Python `cloud/team_mesh.py` implementation. `SyncEnvelope`
//! frames personal multi-device sync streams, and `ElectionVote` participates
//! in failover elections fenced by `host_epoch`.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

/// Post-turn atomic replication bundle broadcast by the Primary Host.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct TurnCommitBundle {
    pub project_session_id: String,
    pub host_epoch: u64,
    pub turn_seq: u64,
    pub cow_delta_sha256: String,
    pub ledger_chain_tip: String,
    pub memory_delta_sha256: String,
    pub proof_merkle_root: String,
}

impl TurnCommitBundle {
    /// Canonical JSON SHA-256 digest, identical to `TurnCommitBundle.compute_digest`
    /// in Python `cloud/team_mesh.py` (sorted keys, compact separators).
    pub fn compute_digest(&self) -> String {
        let value = serde_json::to_value(self).expect("bundle serializes");
        let canonical = canonical_json_string(&value);
        let mut hasher = Sha256::new();
        hasher.update(canonical.as_bytes());
        hex(&hasher.finalize())
    }
}

pub const TURN_COMMIT_VERSION: u32 = 1;
pub const TURN_COMMIT_DOMAIN: &[u8] = b"hex/turn-commit-bundle/v1\0";

/// Domain-separated peer signature around the unchanged seven-field bundle.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct SignedTurnCommitBundle {
    pub bundle: TurnCommitBundle,
    pub author_device_record_id: String,
    pub protocol_version: u32,
    pub signature: String,
}

impl SignedTurnCommitBundle {
    pub fn signing_payload(&self) -> serde_json::Value {
        serde_json::json!({
            "bundle": self.bundle,
            "author_device_record_id": self.author_device_record_id,
            "protocol_version": self.protocol_version,
        })
    }

    pub fn signing_bytes(&self) -> Vec<u8> {
        let mut bytes = TURN_COMMIT_DOMAIN.to_vec();
        bytes.extend_from_slice(canonical_json_string(&self.signing_payload()).as_bytes());
        bytes
    }
}

/// Serialize a JSON value with sorted keys and compact separators, matching
/// Python's `json.dumps(payload, sort_keys=True, separators=(",", ":"))`.
pub fn canonical_json_string(value: &Value) -> String {
    fn write(value: &Value, out: &mut String) {
        match value {
            Value::Object(map) => {
                let entries: std::collections::BTreeMap<&String, &Value> = map.iter().collect();
                out.push('{');
                for (i, (key, val)) in entries.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    out.push_str(&serde_json::to_string(key).expect("key serializes"));
                    out.push(':');
                    write(val, out);
                }
                out.push('}');
            }
            Value::Array(items) => {
                out.push('[');
                for (i, item) in items.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    write(item, out);
                }
                out.push(']');
            }
            other => out.push_str(
                &serde_json::to_string(other).expect("value serializes"),
            ),
        }
    }
    let mut out = String::new();
    write(value, &mut out);
    out
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// Multiplexed personal sync streams between a user's own devices.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SyncStream {
    /// Hash-chained `ledger.db` task events.
    Ledger,
    /// `USER.md`, `SOUL.md`, `MEMORY.md`, `GUARDRAILS.md`, and skills.
    Cognition,
    /// Opt-in workspace diffs and snapshots.
    Workspace,
}

/// Frame for one sync increment sent over an E2EE P2P tunnel.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct SyncEnvelope {
    pub stream: SyncStream,
    /// Account id, team id, or project session id the stream is scoped to.
    pub scope: String,
    pub sender_device_record_id: String,
    /// Inclusive lower bound of the covered event range (e.g. ledger seq).
    pub since_seq: u64,
    pub payload: Value,
}

/// A Replica Peer's vote during a Primary Host failover election.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct ElectionVote {
    pub project_session_id: String,
    pub voter_device_record_id: String,
    pub candidate_device_record_id: String,
    /// The `host_epoch` this vote belongs to (must be current + 1).
    pub election_epoch: u64,
    pub max_known_turn_seq: u64,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_bundle() -> TurnCommitBundle {
        TurnCommitBundle {
            project_session_id: "proj_1".to_string(),
            host_epoch: 1,
            turn_seq: 1,
            cow_delta_sha256: "sha256:cow1".to_string(),
            ledger_chain_tip: "sha256:tip1".to_string(),
            memory_delta_sha256: "sha256:mem1".to_string(),
            proof_merkle_root: "sha256:proof1".to_string(),
        }
    }

    #[test]
    fn digest_matches_python_canonical_form() {
        // Golden value computed with cloud/team_mesh.py's json.dumps(sort_keys=True,
        // separators=(",", ":")) + sha256 contract.
        let expected = "36a8494e1cb9213d4933f86a44beba467b94682d0b8751144cc04b0a0bf176fb";
        assert_eq!(sample_bundle().compute_digest(), expected);
    }

    #[test]
    fn parses_python_style_snake_case_json() {
        let raw = r#"{
            "stream": "ledger",
            "scope": "usr_1",
            "sender_device_record_id": "dev_a",
            "since_seq": 42,
            "payload": {"events": [{"seq": 43}]}
        }"#;
        let env: SyncEnvelope = serde_json::from_str(raw).unwrap();
        assert_eq!(env.stream, SyncStream::Ledger);
        assert_eq!(env.since_seq, 42);
    }

    #[test]
    fn signed_bundle_uses_domain_separated_python_contract() {
        let signed = SignedTurnCommitBundle {
            bundle: sample_bundle(),
            author_device_record_id: "dev_a".to_string(),
            protocol_version: TURN_COMMIT_VERSION,
            signature: format!("ed25519:{}", "A".repeat(86)),
        };
        let expected = concat!(
            "hex/turn-commit-bundle/v1\0",
            r#"{"author_device_record_id":"dev_a","bundle":{"cow_delta_sha256":"sha256:cow1","host_epoch":1,"ledger_chain_tip":"sha256:tip1","memory_delta_sha256":"sha256:mem1","project_session_id":"proj_1","proof_merkle_root":"sha256:proof1","turn_seq":1},"protocol_version":1}"#
        );
        assert_eq!(signed.signing_bytes(), expected.as_bytes());
        let mut hasher = Sha256::new();
        hasher.update(signed.signing_bytes());
        assert_eq!(hex(&hasher.finalize()), "5e05bd534f83fa785ce3a89076b36b97d17de89b6c932db36cc31234f944e7de");
    }

    #[test]
    fn canonical_json_matches_python_for_nested_values() {
        let value: Value = serde_json::from_str(
            r#"{"b":1,"a":{"d":[2,1],"c":true},"e":null}"#,
        )
        .unwrap();
        assert_eq!(
            canonical_json_string(&value),
            r#"{"a":{"c":true,"d":[2,1]},"b":1,"e":null}"#
        );
    }
}
