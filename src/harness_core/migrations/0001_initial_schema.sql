-- PostgreSQL 16+ Authoritative Schema for Custom Autonomous AI Harness (PRD Section 10)
-- Compatible with SQLite WAL bootstrap adapter for zero-config local execution.

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    parent_task_id TEXT,
    project_id TEXT,
    goal TEXT NOT NULL,
    execution_mode TEXT NOT NULL DEFAULT 'standard', -- 'standard' | 'fast_path'
    status TEXT NOT NULL DEFAULT 'SUBMITTED',
    priority INTEGER NOT NULL DEFAULT 50,
    deadline_at TEXT,
    budget_json TEXT NOT NULL DEFAULT '{}',
    constraints_json TEXT NOT NULL DEFAULT '{}',
    deliverable TEXT,
    policy_snapshot_id TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    active_workspace_tree_sha TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    terminal_reason TEXT
);

CREATE TABLE IF NOT EXISTS task_events (
    event_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    attempt_id TEXT,
    sequence_no INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    prior_state TEXT,
    next_state TEXT,
    reason_code TEXT,
    workspace_tree_sha TEXT,
    policy_version TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    actor TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    event_sha256 TEXT NOT NULL,
    UNIQUE (task_id, sequence_no)
);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    parent_attempt_id TEXT,
    branch_label TEXT,
    worker_id TEXT NOT NULL,
    lease_id TEXT,
    fencing_token INTEGER NOT NULL,
    base_tree_sha TEXT,
    head_tree_sha TEXT,
    workspace_path TEXT,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    started_at TEXT NOT NULL,
    ended_at TEXT,
    failure_class TEXT,
    retry_index INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    revision INTEGER NOT NULL,
    dag_json TEXT NOT NULL,
    blast_radius_checks_json TEXT NOT NULL DEFAULT '[]',
    created_by TEXT NOT NULL,
    rationale TEXT NOT NULL,
    created_at TEXT NOT NULL,
    supersedes_revision INTEGER,
    UNIQUE (task_id, revision)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
    plan_revision INTEGER NOT NULL,
    completed_steps_json TEXT NOT NULL DEFAULT '[]',
    pending_steps_json TEXT NOT NULL DEFAULT '[]',
    workspace_tree_sha TEXT NOT NULL,
    snapshot_path TEXT NOT NULL,
    artifact_uri TEXT,
    sha256 TEXT NOT NULL,
    schema_version TEXT NOT NULL DEFAULT '1.0',
    created_at TEXT NOT NULL,
    validity TEXT NOT NULL DEFAULT 'VALID'
);

CREATE TABLE IF NOT EXISTS tool_calls (
    call_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    attempt_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    sequence_in_step INTEGER NOT NULL,
    tool_id TEXT NOT NULL,
    risk_class TEXT NOT NULL,
    canonical_args_hash TEXT NOT NULL,
    arguments_redacted_json TEXT NOT NULL,
    result_json TEXT,
    result_ref TEXT,
    replayed_from_call_id TEXT,
    policy_decision TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING'
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_lookup
    ON tool_calls (task_id, step_id, tool_id, canonical_args_hash, status);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    attempt_id TEXT,
    step_id TEXT,
    action_type TEXT NOT NULL,
    action_hash TEXT NOT NULL,
    action_params_json TEXT NOT NULL DEFAULT '{}',
    blast_radius_summary TEXT NOT NULL DEFAULT '',
    requested_by TEXT NOT NULL,
    decided_by TEXT,
    decision TEXT NOT NULL DEFAULT 'PENDING', -- 'PENDING' | 'APPROVED' | 'REJECTED'
    expires_at TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    comment TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    kind TEXT NOT NULL, -- 'deliverable' | 'cow_delta' | 'proof_bundle' | 'skill_scan_report'
    uri TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    media_type TEXT NOT NULL DEFAULT 'application/json',
    sensitivity TEXT NOT NULL DEFAULT 'internal',
    retention_until TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proof_bundles (
    proof_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    attempt_id TEXT NOT NULL,
    git_diff_sha TEXT NOT NULL,
    git_diff_text TEXT NOT NULL DEFAULT '',
    workspace_tree_sha TEXT NOT NULL,
    runtime_digest TEXT NOT NULL,
    ast_impact_json TEXT NOT NULL DEFAULT '{}',
    verification_results_json TEXT NOT NULL DEFAULT '{}',
    ledger_signature TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skills (
    skill_id TEXT NOT NULL,
    revision_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    disk_path TEXT NOT NULL,
    git_commit_sha TEXT NOT NULL DEFAULT 'local',
    bundle_sha256 TEXT NOT NULL,
    risk_class TEXT NOT NULL DEFAULT 'R1',
    allowed_tools_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'candidate', -- 'candidate' | 'active' | 'pinned' | 'disabled' | 'quarantined'
    latest_scan_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skill_scans (
    scan_id TEXT PRIMARY KEY,
    skill_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    bundle_sha256 TEXT NOT NULL,
    scanner_version TEXT NOT NULL,
    semantic_model_id TEXT NOT NULL,
    risk_score INTEGER NOT NULL,
    severity TEXT NOT NULL, -- 'info' | 'low' | 'medium' | 'high' | 'critical'
    matched_rules_json TEXT NOT NULL DEFAULT '[]',
    osv_vulns_json TEXT NOT NULL DEFAULT '[]',
    semantic_alignment_passed INTEGER NOT NULL DEFAULT 1,
    semantic_notes TEXT NOT NULL DEFAULT '',
    gate_decision TEXT NOT NULL, -- 'ALLOW' | 'REQUIRE_APPROVAL' | 'QUARANTINE'
    scanned_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    machine_class TEXT NOT NULL, -- 'm4_air' | 'desktop_gpu' | 'server'
    capabilities_json TEXT NOT NULL DEFAULT '{}',
    cow_backend TEXT NOT NULL,
    local_llm_models_json TEXT NOT NULL DEFAULT '[]',
    trust_level TEXT NOT NULL DEFAULT 'trusted_local',
    health TEXT NOT NULL DEFAULT 'HEALTHY',
    last_heartbeat_at TEXT NOT NULL,
    registered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leases (
    lease_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    attempt_id TEXT NOT NULL,
    worker_id TEXT NOT NULL REFERENCES workers(worker_id),
    fencing_token INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    renewed_at TEXT NOT NULL,
    released_at TEXT
);

CREATE TABLE IF NOT EXISTS memories (
    memory_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL, -- 'project' | 'personal' | 'episodic'
    project_id TEXT,
    subject TEXT NOT NULL,
    content TEXT NOT NULL,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    confidence REAL NOT NULL DEFAULT 1.0,
    sensitivity TEXT NOT NULL DEFAULT 'internal',
    status TEXT NOT NULL DEFAULT 'active', -- 'candidate' | 'active' | 'superseded' | 'rejected' | 'expired'
    expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS harness_revisions (
    revision_id TEXT PRIMARY KEY,
    parent_revision_id TEXT,
    bundle_hash TEXT NOT NULL,
    bundle_uri TEXT NOT NULL,
    editable_diff_json TEXT NOT NULL DEFAULT '{}',
    skill_scan_id TEXT,
    status TEXT NOT NULL DEFAULT 'candidate', -- 'candidate' | 'active' | 'rejected' | 'rolled_back'
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    promoted_at TEXT
);

CREATE TABLE IF NOT EXISTS evaluations (
    evaluation_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES harness_revisions(revision_id),
    suite_id TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    model_pins_json TEXT NOT NULL DEFAULT '{}',
    results_uri TEXT NOT NULL,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    gate_decision TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reviewer_id TEXT
);
