//! Shared wire types for the HEX control plane (`hex-backend`).
//!
//! These mirror the JSON contracts emitted by hex-backend: snake_case keys,
//! Ed25519-signed envelopes, and the server identity document. All envelopes
//! are verified client-side against the public key published at
//! `GET /v1/server/pubkey` — the client embeds no signing secret.

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// Server identity document from `GET /v1/server/pubkey` (also embedded in `/health`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct ServerPubkey {
    pub algorithm: String,
    pub public_key: String,
    pub key_id: String,
}

/// Generic Ed25519-signed envelope wrapping an arbitrary JSON payload.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct SignedEnvelope {
    pub payload: Value,
    pub signature: String,
    #[serde(default)]
    pub key_id: String,
    #[serde(default)]
    pub etag: String,
}

impl SignedEnvelope {
    pub fn signature_scheme(&self) -> &str {
        self.signature.split(':').next().unwrap_or("")
    }
}

/// Device enrollment request body for `POST /v1/devices/register`.
///
/// The device proves possession of its Ed25519 private key by signing a
/// single-use server challenge over the canonical enrollment payload.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct DeviceEnrollment {
    pub encrypted_device_id: String,
    /// Hex-encoded Ed25519 public key, `ed25519:<64 hex chars>`.
    pub device_pubkey: String,
    pub platform: String,
    #[serde(default = "default_true")]
    pub host_eligible: bool,
    pub challenge: String,
    /// Base64url Ed25519 signature over the canonical enrollment payload.
    pub device_signature: String,
}

fn default_true() -> bool {
    true
}

/// Monotonic Primary Host lease for a shared project session (`host_epoch` fencing).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct HostLease {
    pub project_session_id: String,
    pub team_id: String,
    pub host_epoch: u64,
    pub primary_device_record_id: String,
    pub last_committed_turn_seq: u64,
    pub updated_at: u64,
}

/// Independently versioned component spec inside a channel release manifest.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct ComponentSpec {
    pub latest_version: String,
    pub min_supported_version: String,
    #[serde(default)]
    pub yanked_versions: Vec<String>,
    #[serde(default)]
    pub rollback_to: Option<String>,
    #[serde(default)]
    pub yank_reason: Option<String>,
    #[serde(default)]
    pub rollout_percentage: u32,
    #[serde(default)]
    pub mandatory: bool,
    #[serde(default)]
    pub release_notes: String,
    #[serde(default)]
    pub artifacts: Value,
    #[serde(default)]
    pub requires: Value,
}

/// Channel release manifest payload covering all 8 HEX components.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct ReleaseManifest {
    #[serde(default)]
    pub manifest_version: String,
    pub channel: String,
    pub components: std::collections::BTreeMap<String, ComponentSpec>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_backend_host_lease_json() {
        let raw = r#"{
            "project_session_id": "proj_1",
            "team_id": "team_1",
            "host_epoch": 2,
            "primary_device_record_id": "dev_peer",
            "last_committed_turn_seq": 7,
            "updated_at": 1764000000
        }"#;
        let lease: HostLease = serde_json::from_str(raw).unwrap();
        assert_eq!(lease.host_epoch, 2);
        assert_eq!(lease.last_committed_turn_seq, 7);
    }

    #[test]
    fn parses_enrollment_with_default_host_eligible() {
        let raw = r#"{
            "encrypted_device_id": "dev_enc_v1:abc",
            "device_pubkey": "ed25519:00",
            "platform": "windows",
            "challenge": "hex_ch_1",
            "device_signature": "sig"
        }"#;
        let enroll: DeviceEnrollment = serde_json::from_str(raw).unwrap();
        assert!(enroll.host_eligible);
    }
}
