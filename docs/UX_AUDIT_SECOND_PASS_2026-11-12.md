# HEX UX audit — second pass

Audit date: 12 November 2026. Scope: post-repair implementation currently in the
worktree. These are additional or still-incomplete UX issues; they are not a claim
that the first-pass repairs failed wholesale.

## Findings

| Priority | Finding | Evidence | User impact | Recommended completion condition |
|---|---|---|---|---|
| P0 | Project switch can inherit the preceding project’s model configuration | `src/harness_core/config_loader.py:120-149`; `src/harness_core/orchestrator.py:185-229` | `load_hex_environment(..., override=True)` overwrites keys present in the newly loaded configuration but never removes keys absent from it. `switch_project()` then builds its router with `new_provider or os.getenv("HEX_CLOUD_PROVIDER")` and equivalent model/compressor fallbacks. Switch from project A (local provider/model) to project B (no model section or partial section) and B can execute with A’s selection, rather than global defaults or an explicit “inherited” state. | Keep a managed set of HEX configuration keys and clear/rebuild it from global + target-project configuration on switch. Render each resolved value with its source. |
| P1 | Ctrl-C still reports a confirmed cancellation when cancellation fails | `src/harness_core/tui.py:2217-2227` | The task ID is now available before execution, but `cancel_task()` exceptions are swallowed and the UI unconditionally prints “Task cancelled by operator.” The task may still be running or fail to roll back. | Say “cancellation requested” until the ledger reports `CANCELED`; on error print the error and the task ID with a retry command. |
| P1 | Project task count is incremented before a task exists or is accepted | `src/harness_core/tui.py:2190-2195`; `src/harness_core/cli.py:779-787`, `875-884` | The project card can count a task that fails during submission, is rejected for an empty/policy-invalid goal, or is interrupted before a ledger task is created. The number therefore is neither “submitted tasks” nor “completed tasks.” | Derive the count from the ledger, or increment only after a submission event succeeds and name the metric precisely. |
| P1 | Scheduled-job failures are removed from the retry schedule without a clear operator-facing state | `src/harness_core/scheduler.py:361-392` | `execute_due_jobs()` advances `next_run` even after a provider/task failure. If no notifier is configured, the user receives no alert and the failed run will not retry until the next cron occurrence. | Persist a failed-run status, surface it in `hex schedule list/status`, and clearly choose/configure retry vs. skip semantics. |
| P2 | Model persistence can report a save target that was never written | `src/harness_core/orchestrator.py:156-176`; `src/harness_core/onboard.py:139-172` | If a workspace has no `.hex/config.yaml`, `set_active_model()` names `~/.hex/config.yaml` as the saved path. `update_config_yaml_model()` is a no-op when that file does not exist and does not return success. The TUI can therefore announce a durable save that did not happen. | Have the update function return a boolean/result; create an explicit config file when appropriate; show “session-only” on failure. |
| P2 | `/compressor` and `/skin` look durable but are session-only | `src/harness_core/tui.py:1514-1525`, `1910-1918` | Both commands print “updated” but only mutate the console/router in memory. This is especially confusing beside `/model`, which now saves to project configuration. | Either persist them to the selected scope or label the confirmation “session-only” and offer a durable flag/setting. |
| P2 | Duplicate project-name resolution is safe but not actionable | `src/harness_core/project_registry.py:184-201`; `src/harness_core/tui.py:1960-1967`; `src/harness_core/cli.py:467-484` | A duplicate display name now returns no project, correctly preventing misrouting, but the UI says only “not found.” It does not say that the name is ambiguous or show the accepted path/ID choices. | Return an explicit ambiguous-match result and display candidate names, IDs, and paths. |
| P3 | Task counters remain a separate mutable cache from the ledger | `src/harness_core/project_registry.py:258-270`; task creation in `src/harness_core/db.py:352-501` | Task counts can drift after API/server/scheduler/worker submission, project-registry write failure, deletion, or restore because only selected TUI/CLI routes increment them. | Remove the duplicate counter or recompute it from the ledger using the stable project ID. |

## Validation notes

Focused UX/model/project/TUI/OAuth suites currently pass (81 tests after the prior
repair work). Those tests do not cover switching into a partial/no-config project,
failure of `cancel_task()`, or durable-save failure; add those cases before declaring
the UX repair work complete.
