# HEX vs. Hermes: Fit-and-Finish Audit

**Audited:** 16 November 2026  
**HEX snapshot:** `57cbfdb`, branch `experimental-features`  
**Hermes reference snapshot:** `71b67652`, local checkout under `benchmarks/hermes-agent`  
**Scope:** Product completeness, interaction reliability, distribution, engineering workflow, documentation credibility, and repository organization.

## Executive verdict

**HEX does not primarily have a styling problem. It has a product-completion problem: substantial infrastructure is not consistently connected to the interfaces, installers, and promises presented to users.**

The strongest evidence is not subjective. The desktop startup does not connect its rendered shell to a working chat flow. Some commands exposed through the native-client backend return success text without performing the operation. The release path does not deliver the native clients advertised in the README. Client and server disagree about important streaming events. CI skips 56 existing pytest-style tests, despite those tests passing when actually run.

Those gaps make a product feel unfinished even when the underlying architecture is sophisticated. A user experiences “my message disappeared,” “this command said it worked,” or “the documented launcher does not exist”—not the number of providers, databases, or compression techniques behind the interface.

Hermes is not a pristine repository to copy. It has considerably more root-level clutter, large modules, complicated build infrastructure, and acknowledged test debt. Its more useful advantage is **explicit ownership of user-visible behavior**, supported by shared command definitions, draft-preservation logic, configuration write boundaries, and tests that drive a real application through failure and recovery.

**Recommendation:** Finish and support one complete HEX interaction surface before expanding the product claim. Preserve the backend investment. Stop counting an implemented module, a rendered control, or a passing state-helper test as a shipped feature.

### Fix these before cosmetic redesign

| Order | Gap | Why it matters |
| --- | --- | --- |
| 1 | Commands that announce operations they did not perform | Breaks trust, especially for rollback, memory, and skill inspection. |
| 2 | Desktop presented as finished while its startup and interactions are disconnected | The advertised primary experience cannot be justified by the current entry point. |
| 3 | Installation and release artifacts do not complete the advertised native launch path | A developer checkout can work while a user's installation cannot. |
| 4 | ACP event, session, cancellation, and completion contracts | Daily conversation behavior is inconsistent at the client/backend boundary. |
| 5 | Validation proves components rather than supported user journeys | Green checks can coexist with the failures above. |
| 6 | Documentation and version claims disagree with the implementation | Users and contributors cannot tell what is supported. |
| 7 | Configuration ownership, module boundaries, and visual interaction consistency | Important, but downstream of a truthful and functional product contract. |

Here, a **release blocker** means a blocker for the affected advertised experience—not a finding that every HEX mode is unusable, nor a security-vulnerability classification.

---

## 1. Method and limits

This is a focused source audit with isolated local probes, not a line-by-line review of either entire repository.

Inspected paths included HEX's Python CLI and console, ACP server, Rust TUI and desktop, onboarding, configuration, packaging, CI, documentation, and storage interfaces. Hermes inspection focused on corresponding command/configuration boundaries, desktop composer behavior, design guidance, installers, regression tests, and quality gates.

Evidence is separated into:

- **Source-confirmed:** A behavior or missing connection traceable through the inspected entry points.
- **Probe-confirmed:** Executed locally with temporary directories and mock backends, without real provider calls.
- **Recommendation:** A proposed improvement, not a claim about existing behavior.

The full test suites, live providers, installer scripts, Windows paths, and desktop applications were **not** exercised end to end. No comparative RAM, latency, FPS, or model-quality benchmark was performed. Existing repository audit scores were not treated as evidence. Source links refer to this local workspace; line numbers describe the audited snapshot.

### Local checks actually performed

| Check | Observation | Boundary of the result |
| --- | --- | --- |
| Parse `hex desktop` through the current parser | Rejected, `SystemExit 2` | Confirms the documented subcommand is absent; not a desktop runtime test. |
| Call ACP `/remember`, `/rollback`, and `/scan` with a backend implementing no operations | Success messages returned; scanning a nonexistent directory also succeeded; temporary workspace stayed empty | Confirms these branches do not invoke the promised backing operation. |
| Call ACP `/rewind` and `/lease` | Unknown-command errors | Applies to ACP, not the legacy Python console. |
| Capture ACP notifications from a scripted task | Nested tool updates and `stream_end`/`Ready` terminal updates | Compared with the Rust desktop parser, not with a live provider. |
| Submit two ACP prompts, cancel, then try loading the session in a fresh server | Prior dialogue absent from the second task request; cancellation processed after execution; fresh server cannot load session | Establishes limitations of this ACP boundary, not absence of all persistence in HEX. |
| Discover the credential-pool test module using `unittest` | Zero discovered cases | Its tests are plain pytest-style functions. |
| Run all five affected pytest modules directly | **56 passed in 2.82 seconds** | Only this selected set was run, not the whole suite. |
| Build and inspect a wheel from a temporary copy of tracked packaging inputs | Build succeeded; 553 members; no native executables or vendor members; 304 skill Markdown files; no optional-skill Markdown files | Artifact-content inspection, not a clean-machine installation test. |
| Inspect Git-tracked inventory and source sizes | Counts summarized below | Ignored caches and runtime directories are not counted as committed product structure. |

---

## 2. What HEX should keep

It would be a mistake to conclude that HEX needs to be rewritten or that its engineering work is imaginary.

1. **A namespaced Python package and separate Rust clients are a reasonable foundation.** The broad separation of execution, cognition, storage, clients, and providers is already more intentional than a root full of unrelated scripts.
2. **The native TUI has an actual event loop and backend launch path.** It is materially further along as an integrated surface than the desktop. See [crates/hex-tui/src/main.rs:216](file:///Users/anv./Development/Harness/crates/hex-tui/src/main.rs#L216).
3. **Storage and isolation have concrete implementations.** The snapshot-provider interface and tree hashing are present in [src/harness_core/storage/cow.py:39](file:///Users/anv./Development/Harness/src/harness_core/storage/cow.py#L39); event recording and transactional task claiming exist in [src/harness_core/storage/db.py:803](file:///Users/anv./Development/Harness/src/harness_core/storage/db.py#L803) and [db.py:1845](file:///Users/anv./Development/Harness/src/harness_core/storage/db.py#L1845). This audit does not certify every isolation or performance claim.
4. **Diagnostics are already structured.** Doctor models checks, severity, remediation, and fixability rather than just printing a banner: [src/harness_core/client/doctor.py:28](file:///Users/anv./Development/Harness/src/harness_core/client/doctor.py#L28).
5. **Several features dismissed by older audits now exist.** Quick, automatic, blank-slate, and reasoning-aware setup are present; the Python console has real memory, rewind, and approval-lease dispatch. See [src/harness_core/client/onboard.py:356](file:///Users/anv./Development/Harness/src/harness_core/client/onboard.py#L356) and [src/harness_core/client/tui.py:1735](file:///Users/anv./Development/Harness/src/harness_core/client/tui.py#L1735).
6. **Useful tests exist.** The 56 selected tests passing is positive evidence for the tested components. The immediate deficiency is that the normal gate does not collect them.

The opportunity is to make these capabilities available through a small number of coherent, tested workflows—not to replace them with another framework.

---

## 3. The highest-impact product gaps

### F01. Some commands simulate success instead of doing the work

**Priority: Release blocker for these commands. Evidence: source and local probes.**

In [src/harness_core/client/acp.py:608](file:///Users/anv./Development/Harness/src/harness_core/client/acp.py#L608), several command branches construct reassuring text directly:

- `/scan` claims skills were evaluated and registered.
- `/remember` claims a governed memory was recorded.
- `/reflect` claims architectural lessons were synthesized.
- `/memory` and `/search` report zero matches without the corresponding query operation.
- `/lsp` reports active language servers and zero errors.
- `/fork` announces a fixed branch name.
- `/rollback` announces a pristine working tree.
- `/quota` returns fixed quota/status text.

The isolated probes confirmed `/remember`, `/rollback`, and `/scan` can return success without a functional backend. These are not just unfinished labels: they are user-facing acknowledgments of work that was not performed.

The native TUI delegates nonlocal slash commands to this server at [crates/hex-tui/src/main.rs:756](file:///Users/anv./Development/Harness/crates/hex-tui/src/main.rs#L756). This therefore affects an actual product surface, not only an unused helper.

**Why Hermes is instructive:** Its command model explicitly carries execution handlers, aliases, busy policies, and desktop dispositions in one registry: [benchmarks/hermes-agent/hermes_cli/commands.py:44](file:///Users/anv./Development/Harness/benchmarks/hermes-agent/hermes_cli/commands.py#L44). The valuable principle is that discoverability and execution share a contract.

**Recommendation:** Remove unsupported commands from discovery or return a clear unsupported error until connected to real operations. Never use plausible success text as a temporary implementation, especially for safety-related state.

**Done when:** Every successful mutating command produces independently inspectable state; every query reflects actual state; negative tests cover nonexistent directories, absent sessions, and failing dependencies. Acknowledgment, progress, and successful completion are distinct responses.

### F02. The desktop is a rendered shell, not a completed interaction surface

**Priority: Release blocker for the desktop claim. Evidence: source wiring.**

[crates/hex-desktop/src/main.rs:1](file:///Users/anv./Development/Harness/crates/hex-desktop/src/main.rs#L1) initializes the theme and opens `DesktopWindow::new()`. It does not connect the ACP bridge, subscribe to backend events, or register the shortcut initializer.

The shell starts with `acp_bridge: None`. Bridge attachment and event-consumption methods exist, but are not connected by this production startup: [crates/hex-desktop/src/ui/shell.rs:59](file:///Users/anv./Development/Harness/crates/hex-desktop/src/ui/shell.rs#L59).

Additional signs of component scaffolding rather than integrated interaction:

- The composer renders its stored text or placeholder in ordinary divs, not an editable input implementation: [crates/hex-desktop/src/ui/composer.rs:120](file:///Users/anv./Development/Harness/crates/hex-desktop/src/ui/composer.rs#L120).
- Copy, Regenerate, and Branch have pointer cursors and hover styles but no action handlers: [crates/hex-desktop/src/ui/message.rs:155](file:///Users/anv./Development/Harness/crates/hex-desktop/src/ui/message.rs#L155).
- New-chat/session-selection helpers change local shell state rather than establishing or loading a backend conversation: [shell.rs:172](file:///Users/anv./Development/Harness/crates/hex-desktop/src/ui/shell.rs#L172).
- Shortcut bindings are constructed, but their initializer is not called from startup: [crates/hex-desktop/src/shortcuts.rs:109](file:///Users/anv./Development/Harness/crates/hex-desktop/src/shortcuts.rs#L109).
- Tests directly call shell methods and inject events, bypassing the missing user-input/backend path: [shell.rs:321](file:///Users/anv./Development/Harness/crates/hex-desktop/src/ui/shell.rs#L321).

A source search found no desktop `on_click`, `on_action`, `on_key_down`, `EntityInputHandler`, or `FocusHandle` use. This is a concrete wiring finding; it is not a visual assessment from launching GPUI.

**Why it feels unfinished:** Controls communicate affordances they do not fulfill. A convincing static layout amplifies that mismatch.

**Recommendation:** Label desktop experimental now. Either finish a narrow chat vertical slice—editable composer, real session, streaming, tool lifecycle, completion, cancel, error recovery—or keep it out of the default product promise. Do not spend the next iteration adding more decorative panels.

**Done when:** An acceptance test launches the real entry point, types through the real input system, submits to the real backend with a scripted model, observes completion, and exercises copy, cancel, session switching, and an error state.

### F03. Distribution does not deliver the product described in the README

**Priority: Release blocker for the advertised native installation. Evidence: source, parser probe, wheel build.**

The documented `hex desktop` command is not accepted by [src/harness_core/client/cli.py:57](file:///Users/anv./Development/Harness/src/harness_core/client/cli.py#L57). Meanwhile, [README.md:97](file:///Users/anv./Development/Harness/README.md#L97) and [docs/RUNBOOK.md:60](file:///Users/anv./Development/Harness/docs/RUNBOOK.md#L60) advertise native launch paths.

The POSIX installer installs Python and links Python entry points, but does not install or build the Rust clients: [install.sh:112](file:///Users/anv./Development/Harness/install.sh#L112). The TUI launcher searches checkout build directories, PATH, and a user binary directory; otherwise it tries a source-tree Cargo build: [src/harness_core/client/tui_launcher.py:13](file:///Users/anv./Development/Harness/src/harness_core/client/tui_launcher.py#L13).

That fallback assumes an environment resembling a development checkout. It is not a reliable distribution strategy for someone who installed a wheel and runs HEX from an unrelated project.

The wheel build confirmed:

- A Python-only wheel with no native clients.
- No bundled vendor members.
- Core skill Markdown packaged, but no optional-skill Markdown.
- One packaged SQL migration—not an absence of all migration data.

The relevant packaging rules are [pyproject.toml:62](file:///Users/anv./Development/Harness/pyproject.toml#L62). The release workflow publishes Python distributions and the installer, not native-client artifacts: [.github/workflows/release.yml:84](file:///Users/anv./Development/Harness/.github/workflows/release.yml#L84).

A universal Python wheel does not have to contain platform-specific applications. **The problem is the absence of a complete alternative delivery path.** Optional resources can also be intentionally excluded, but the product must state how they become available.

There is a separate static Windows concern: the documented remote-script invocation and the script's local-checkout assumptions do not obviously align. See [install.ps1:14](file:///Users/anv./Development/Harness/install.ps1#L14). Windows execution was not tested here.

**Hermes reference:** Its installer makes runtime versions and verified downloads explicit: [benchmarks/hermes-agent/setup-hermes.sh:105](file:///Users/anv./Development/Harness/benchmarks/hermes-agent/setup-hermes.sh#L105). Its desktop core workflow packages the application and requires a packaged-app first-chat smoke test: [benchmarks/hermes-agent/.github/workflows/e2e-desktop-core.yml:80](file:///Users/anv./Development/Harness/benchmarks/hermes-agent/.github/workflows/e2e-desktop-core.yml#L80).

**Recommendation:** Define one install contract per supported surface. Deliver matching native artifacts or make a Python-only experience the explicitly supported default. Keep source builds a developer option, not an implicit rescue path.

**Done when:** A release artifact installs and completes its documented first task with an isolated home, outside the source checkout, without a previously installed HEX binary or undeclared compiler. Upgrade and uninstall behavior should then be tested against that same contract.

### F04. Desktop and backend disagree about the wire protocol

**Priority: High; required before finishing desktop. Evidence: source and captured notifications.**

| Event | Python ACP server emits | Desktop bridge expects |
| --- | --- | --- |
| Text | `session/update`, `updateType=agent_response`, `delta` | Handles a delta. |
| Tools | `session/update`, `updateType=tool`, nested tool payload | Handles separate `tool/call` or `tools/call`, and corresponding result methods. |
| End of stream | `session/update`, `updateType=stream_end`, `status=Ready` | Looks for update type `done` or status `complete`. |
| Final prompt result | JSON-RPC response with task result | Awaits the response but discards it rather than converting it into completion state. |

Sources: [acp.py:265](file:///Users/anv./Development/Harness/src/harness_core/client/acp.py#L265), [crates/hex-desktop/src/acp_bridge.rs:247](file:///Users/anv./Development/Harness/crates/hex-desktop/src/acp_bridge.rs#L247), and [acp_bridge.rs:342](file:///Users/anv./Development/Harness/crates/hex-desktop/src/acp_bridge.rs#L342).

Wiring the existing desktop bridge into startup would therefore not, by itself, finish the product. Tool cards and terminal status still need a shared protocol contract. Independently written mock-server tests can pass while each side speaks a different protocol.

**Recommendation:** Establish one versioned internal event schema with explicit terminal states. Test the actual Python server against the Rust consumer using captured or scripted real-server fixtures. Validate advertised external ACP compatibility separately; do not equate JSON-RPC transport with protocol conformance.

**Done when:** A real client/server test observes text, tool start/result/error, approval, completion, and cancellation with no invented mock-only event shapes.

### F05. Conversation lifecycle is weaker than the session UI implies

**Priority: High. Evidence: source and isolated ACP probes.**

At the ACP boundary:

- Sessions are held in an in-memory dictionary, and load only finds a session in that process: [acp.py:31](file:///Users/anv./Development/Harness/src/harness_core/client/acp.py#L31), [acp.py:202](file:///Users/anv./Development/Harness/src/harness_core/client/acp.py#L202).
- History is appended after a turn, but the next task request contains the current prompt and `surface=acp`, not prior dialogue or a conversation identifier: [acp.py:215](file:///Users/anv./Development/Harness/src/harness_core/client/acp.py#L215), [acp.py:356](file:///Users/anv./Development/Harness/src/harness_core/client/acp.py#L356).
- Cancellation sets a flag, while the stdio request loop executes requests synchronously. A cancel request cannot be read while task execution occupies that loop: [acp.py:391](file:///Users/anv./Development/Harness/src/harness_core/client/acp.py#L391), [acp.py:656](file:///Users/anv./Development/Harness/src/harness_core/client/acp.py#L656).

The native TUI adds its own inconsistencies:

- A supplied session ID is overwritten by an unconditional new-session request in the inspected startup path: [main.rs:203](file:///Users/anv./Development/Harness/crates/hex-tui/src/main.rs#L203).
- After streamed text arrives, a gap of over 350 ms can mark the application ready without an authoritative terminal event: [main.rs:362](file:///Users/anv./Development/Harness/crates/hex-tui/src/main.rs#L362).
- Ctrl+C quits the application in the key handler rather than providing a complete in-flight cancellation journey: [main.rs:395](file:///Users/anv./Development/Harness/crates/hex-tui/src/main.rs#L395).

These findings do not erase HEX's task ledger or memory system. They show that task persistence is not automatically durable conversation continuity.

**Hermes reference:** A targeted regression test forces a socket reconnect race between replay and resume, then checks that the full reply becomes visible and the stop button disappears: [benchmarks/hermes-agent/apps/desktop/e2e/core/reply-tail-after-reconnect.spec.ts:63](file:///Users/anv./Development/Harness/benchmarks/hermes-agent/apps/desktop/e2e/core/reply-tail-after-reconnect.spec.ts#L63). This is the granularity at which users notice polish.

**Recommendation:** Define a conversation lifecycle independently from individual tasks: submission accepted, running, waiting for approval, cancelling, completed, failed, disconnected. Let backend terminal events own completion. Add durable transcript/resume semantics and concurrent cancellation handling.

**Done when:** A two-turn conversation preserves context, reconnect produces no lost or duplicated text, restart can resume a supported session, and cancel reaches the active execution rather than only updating a client flag.

### F06. Different surfaces expose different products under the same name

**Priority: High. Evidence: source and command probes.**

The legacy Python console contains real implementations for several capabilities that are unavailable or simulated in ACP. Native TUI discovery is separately hardcoded in [crates/hex-tui/src/app.rs:163](file:///Users/anv./Development/Harness/crates/hex-tui/src/app.rs#L163). The README presents a broadly unified slash-command experience without adequately distinguishing those surfaces.

Startup also changes the onboarding path. The CLI launches the native TUI and returns before the later Python missing-credentials and workspace-selection flow: [cli.py:774](file:///Users/anv./Development/Harness/src/harness_core/client/cli.py#L774). Tests using `HEX_TESTING` bypass that default native route: [cli.py:765](file:///Users/anv./Development/Harness/src/harness_core/client/cli.py#L765).

**User impact:** A command can work in one mode, be unknown in another, and claim success without doing work in a third. Testing one surface does not establish the behavior of `hex` as launched by a new user.

**Recommendation:** Share command metadata and backend operations; keep rendering surface-specific. Publish a capability matrix with supported, experimental, and unavailable states. Start by supporting fewer commands honestly rather than reproducing the entire Python dispatcher in Rust.

**Done when:** Help, completion, documentation, and dispatch agree per surface. First-run tests exercise the actual default launch route without a test flag silently substituting a different product.

---

## 4. Why the engineering process currently permits these gaps

### F07. CI's definition of success is narrower than the product's claims

**Priority: High. Evidence: CI source and executed tests.**

The primary workflow builds the TUI, then invokes `python -m unittest discover -v tests`: [.github/workflows/ci.yml:29](file:///Users/anv./Development/Harness/.github/workflows/ci.yml#L29). The release workflow uses the same discovery approach.

Five modules contain 56 plain pytest-style functions without `load_tests` adapters:

| Module | Tests missed by that discovery style |
| --- | ---: |
| [tests/test_approval_lease.py:25](file:///Users/anv./Development/Harness/tests/test_approval_lease.py#L25) | 11 |
| [tests/test_credential_pool.py:43](file:///Users/anv./Development/Harness/tests/test_credential_pool.py#L43) | 20 |
| [tests/test_mobile_reaction_listener.py](file:///Users/anv./Development/Harness/tests/test_mobile_reaction_listener.py) | 9 |
| [tests/test_timeline_rewind.py](file:///Users/anv./Development/Harness/tests/test_timeline_rewind.py) | 8 |
| [tests/test_title_synthesis.py](file:///Users/anv./Development/Harness/tests/test_title_synthesis.py) | 8 |
| **Total** | **56** |

All 56 passed under direct pytest execution. This is a collection/gating defect, not evidence that those components are broken.

The primary workflow also has no Rust test invocation, desktop build, lint/type check, or documentation-drift check. Its OS/Python matrix omits Windows and Python 3.10 despite corresponding support claims. A TUI build and `--help`/doctor smoke cannot prove chat, interaction, or packaged installation works.

The TUI's headless `--check` renders synthetic frames: [main.rs:99](file:///Users/anv./Development/Harness/crates/hex-tui/src/main.rs#L99). Such checks are useful but must not be presented as end-to-end verification.

**Hermes mechanisms worth borrowing:**

- Blocking selected lint/static guards plus a per-unit code-health ratchet; type checking is advisory, not uniformly blocking: [benchmarks/hermes-agent/.github/workflows/lint.yml:1](file:///Users/anv./Development/Harness/benchmarks/hermes-agent/.github/workflows/lint.yml#L1).
- Explicit complexity/size targets and rules against recurring failure patterns: [benchmarks/hermes-agent/scripts/code_health/config.py:18](file:///Users/anv./Development/Harness/benchmarks/hermes-agent/scripts/code_health/config.py#L18).
- Documentation drift checks that tolerate a baseline but reject new gaps: [benchmarks/hermes-agent/tests/ci/test_docs_drift.py:1](file:///Users/anv./Development/Harness/benchmarks/hermes-agent/tests/ci/test_docs_drift.py#L1).
- A real application plus real gateway with a scripted loopback model, rather than mocking away the integration: [e2e-desktop-core.yml:3](file:///Users/anv./Development/Harness/benchmarks/hermes-agent/.github/workflows/e2e-desktop-core.yml#L3).

**Recommendation:** Standardize Python collection, run Rust tests, build each supported surface, and introduce a small acceptance lane. Baseline existing static debt rather than delaying all improvements until a cleanup rewrite.

**Done when:** CI reports collected tests clearly and fails for a missing supported artifact, nonexistent documented command, protocol mismatch, or broken first-chat journey.

### F08. Configuration behavior depends on incidental environment state

**Priority: Medium-high. Evidence: source and parser probe.**

[src/harness_core/config_loader.py:31](file:///Users/anv./Development/Harness/src/harness_core/config_loader.py#L31) optionally imports PyYAML. If unavailable, [config_loader.py:65](file:///Users/anv./Development/Harness/src/harness_core/config_loader.py#L65) falls back to a small indentation parser. PyYAML is not declared in the package dependencies.

The fallback was active in the audit environment. It parsed YAML values `false`, `3`, and `[file, terminal]` as strings rather than a boolean, number, and list. Parsing errors can also be reduced to an empty configuration.

This establishes inconsistent types and hidden configuration failure; it does not establish a particular downstream security bypass.

Onboarding also has direct configuration write paths: [onboard.py:180](file:///Users/anv./Development/Harness/src/harness_core/client/onboard.py#L180) and [onboard.py:1270](file:///Users/anv./Development/Harness/src/harness_core/client/onboard.py#L1270).

**Hermes reference:** Its shared writer distinguishes patching from replacement, refuses to overwrite after failed reads, checks unintended omitted settings, and preserves user-authored configuration structure: [benchmarks/hermes-agent/hermes_cli/config.py:1798](file:///Users/anv./Development/Harness/benchmarks/hermes-agent/hermes_cli/config.py#L1798). Static guards enforce the read/write ownership boundary.

**Recommendation:** Use one supported parser with typed validation and useful errors. Establish one atomic configuration-writing seam and explicit precedence rules. Add multi-profile infrastructure only when the product actually needs it; the immediate requirement is predictable single-user configuration.

**Done when:** Identical configuration produces identical typed values across supported installs; malformed input is actionable; a settings update cannot silently erase unrelated user settings.

### F09. Onboarding verification reports more than it actually verifies

**Priority: High for truthful readiness; medium for presentation. Evidence: source.**

HEX does have onboarding and a first-task verification flow. The problem is its claim boundary.

[onboard.py:211](file:///Users/anv./Development/Harness/src/harness_core/client/onboard.py#L211) creates workspace directories and a marker, probes installed tools, writes diagnostic/CAS artifacts, and records a ledger task. Those are useful checks. But it then writes “ALL SUBSYSTEMS OPERATIONAL (PASS),” transitions the task directly through success, and prints that CoW isolation passed without actually proving an isolated model-driven edit/test/patch journey: [onboard.py:258](file:///Users/anv./Development/Harness/src/harness_core/client/onboard.py#L258), [onboard.py:294](file:///Users/anv./Development/Harness/src/harness_core/client/onboard.py#L294), [onboard.py:338](file:///Users/anv./Development/Harness/src/harness_core/client/onboard.py#L338).

**Recommendation:** Call this a diagnostic smoke test and report exactly what passed, failed, or was not exercised. Offer a separate, explicit provider and workspace-verification task when configured. Never manufacture a successful task record as evidence that the task execution path works.

**Hermes lesson:** Its onboarding flow models pending/shown/done/skipped states and typed events: [benchmarks/hermes-agent/agent/onboarding_flow.py:17](file:///Users/anv./Development/Harness/benchmarks/hermes-agent/agent/onboarding_flow.py#L17). The useful distinction is explicit progress and user choice, not necessarily a more theatrical wizard.

**Done when:** Setup accurately distinguishes “configuration saved,” “provider reachable,” “storage writable,” and “isolated task completed”; skip and rerun are supported without misleading all-green summaries.

---

## 5. Documentation, identity, and visual polish

### F10. Documentation currently weakens credibility

**Priority: High; inexpensive relative to backend work. Evidence: source and parser probe.**

The documentation asserts a finished product more strongly than the inspected implementation supports:

- The README advertises a finished native desktop, memory/FPS numbers, and a hardcoded passing-test badge: [README.md:15](file:///Users/anv./Development/Harness/README.md#L15), [README.md:132](file:///Users/anv./Development/Harness/README.md#L132).
- [docs/COMPARISON.md:34](file:///Users/anv./Development/Harness/docs/COMPARISON.md#L34) claims “100% feature parity.” Its claim that Hermes has only linear history conflicts with Hermes's registered rewind, branch, and rollback commands: [commands.py:83](file:///Users/anv./Development/Harness/benchmarks/hermes-agent/hermes_cli/commands.py#L83).
- A `target_fps = 120` configuration default is not an FPS measurement; the window options do not consume that setting: [crates/hex-desktop/src/window.rs:26](file:///Users/anv./Development/Harness/crates/hex-desktop/src/window.rs#L26), [window.rs:75](file:///Users/anv./Development/Harness/crates/hex-desktop/src/window.rs#L75).
- Package metadata says `0.22.0`, while the imported runtime version and ACP identity say `0.20.1`: [pyproject.toml:7](file:///Users/anv./Development/Harness/pyproject.toml#L7), [src/harness_core/__init__.py:8](file:///Users/anv./Development/Harness/src/harness_core/__init__.py#L8), [acp.py:143](file:///Users/anv./Development/Harness/src/harness_core/client/acp.py#L143).
- Contributor examples use the old `harness_core.cli` module location: [docs/CONTRIBUTING.md:31](file:///Users/anv./Development/Harness/docs/CONTRIBUTING.md#L31).
- The README links a root license file that is absent, despite the package declaring Apache-2.0.

Independent component versions can be legitimate. Conflicting user-facing product versions without an explanation are not helpful for support.

**Recommendation:** Replace superiority language with a supported-capability table, reproducible setup instructions, known limitations, and measured benchmarks with workload/hardware/methodology. Mark older audits as historical. Restore the intended license text only after verifying the project's licensing intent and third-party obligations.

**Done when:** A reader can identify the supported install path, default surface, available commands, experimental features, and release version without reconciling contradictory documents.

### F11. Visual identity is being treated as a substitute for interaction design

**Priority: Medium, after functional affordances. Evidence: design/source comparison; not a screenshot audit.**

HEX's desktop borrows Hermes naming, dark-zinc styling, and familiar panels. That does not reproduce Hermes's interaction design. Even the assistant model label defaults to a hardcoded model name: [message.rs:122](file:///Users/anv./Development/Harness/crates/hex-desktop/src/ui/message.rs#L122).

Hermes's [apps/desktop/DESIGN.md:25](file:///Users/anv./Development/Harness/benchmarks/hermes-agent/apps/desktop/DESIGN.md#L25) describes behavioral principles as well as tokens: one home per action, visible feedback, no focus theft from background activity, consistent primitives, approval lifecycle, and accessibility expectations.

Its composer implementation protects user work:

- Captures draft/session ownership before asynchronous submission.
- Journals the outgoing message before the first await.
- Restores rejected text and attachments into the correct session.
- Avoids clearing a draft that changed while submission was pending.

See [benchmarks/hermes-agent/apps/desktop/src/app/chat/composer/hooks/use-composer-submit.ts:126](file:///Users/anv./Development/Harness/benchmarks/hermes-agent/apps/desktop/src/app/chat/composer/hooks/use-composer-submit.ts#L126) and [use-composer-draft.ts:169](file:///Users/anv./Development/Harness/benchmarks/hermes-agent/apps/desktop/src/app/chat/composer/hooks/use-composer-draft.ts#L169).

That is substantive polish: **the application respects the user's words, attention, and understanding of state.**

**Recommendation:** Build HEX's identity around understandable, verified changes rather than competitor visual parity. Prioritize:

1. An editor that handles focus, multiline input, paste, composition, and pending drafts correctly.
2. A readable transcript with real model identity, structured code/tool output, and working copy actions.
3. Unambiguous running, approval, disconnected, failed, and completed states.
4. Keyboard navigation, accessible control semantics, predictable focus, and reduced-motion behavior.
5. A compact visual system with consistent spacing, typography, button hierarchy, and restrained decoration.

Do not try to compensate for missing behavior with more panels, badges, animation, or performance slogans.

---

## 6. Is the repository structure actually amateur?

**Not in the simple sense suggested by the directory tree.** HEX already has a package boundary and separate client crates. Hermes is not cleaner by raw root layout: the observed checkouts had roughly 33 versus 166 visible root entries, and zero versus 87 root-level Python files. These are contextual inventory observations, not quality scores.

### What the tracked inventory actually contains

| HEX area | Tracked files | Interpretation |
| --- | ---: | --- |
| `src` | 1,071 | Dominated by bundled catalogs: 538 optional-skill and 440 skill files, not 1,071 core implementation modules. |
| `vendor` | 1,718 | Requires provenance, licensing, update, and distribution ownership. |
| `tests` | 91 | Organization and collection matter more than the count. |
| `crates` | 69 | Sensible place for native clients and shared Rust code. |
| `.gsd` | 202 | Planning/history volume can overwhelm contributor orientation and code search. |
| `UI Redesign Target` | 42 | Design reference material should have a clearly secondary role. |
| `audits` | 7 | Repeated reports need dates, scope, and an authoritative current status. |
| `docs` | 9 | Small enough to maintain coherently; currently contains stale instructions and claims. |

The large Hermes test inventory is not a useful target to reproduce by count. A dozen tests covering HEX's actual installation/chat/recovery contract would address more immediate risk than hundreds of additional state-construction assertions.

### Real structural problems

**1. Behavioral ownership is spread across multiple dispatchers.**

The Python console, ACP server, Rust TUI, and desktop each carry parts of the command/session story. This creates duplicated definitions and contradictory behavior. A shared command service and explicit session contract address the problem more directly than moving every folder.

**2. Several implementation units are too broad to review safely.**

Measured sizes include comments and tests where present:

| Source | Lines | More revealing detail |
| --- | ---: | --- |
| Storage database module | 4,718 | Large persistence surface spanning multiple backends. |
| Python console | 3,895 | Rendering, input, dispatch, and task presentation share one file. |
| Cognition memory module | 3,227 | Multiple memory responsibilities in one module. |
| Core orchestrator | 3,044 | `_execute_standard_dag` spans about 735 lines. |
| Gateway | 2,436 | Tool registration and invocation are concentrated here. |
| CLI | 2,251 | `_main_impl` spans about 1,739 lines; parser construction another 450. |

Representative anchors: [cli.py:509](file:///Users/anv./Development/Harness/src/harness_core/client/cli.py#L509), [src/harness_core/core/orchestrator.py:1818](file:///Users/anv./Development/Harness/src/harness_core/core/orchestrator.py#L1818), [src/harness_core/cognition/memory.py:694](file:///Users/anv./Development/Harness/src/harness_core/cognition/memory.py#L694), [src/harness_core/gateway.py:709](file:///Users/anv./Development/Harness/src/harness_core/gateway.py#L709).

Hermes also has multi-thousand-line modules. The lesson is to prevent further growth and extract along tested responsibilities—not claim that Hermes has solved monoliths or impose arbitrary mass splitting.

**3. Current product truth competes with historical planning.**

Multiple parity/supremacy audits and planning artifacts make it difficult to tell what was proposed, implemented, tested, and shipped. This is a navigation and status-governance problem, not evidence that keeping design history is inherently bad.

**4. Application dependency reproducibility is incomplete.**

Cargo's lockfile is locally present but ignored rather than tracked: [.gitignore:25](file:///Users/anv./Development/Harness/.gitignore#L25). Python dependencies use lower bounds without a corresponding locked release environment in the inspected packaging path: [pyproject.toml:12](file:///Users/anv./Development/Harness/pyproject.toml#L12). For shipped applications, reproducible release inputs matter more than how modern the directory names sound.

**5. Contributor orientation is underdeveloped.**

Contribution guidance exists but has stale commands. There is no root security-reporting policy or license text. These are small trust and onboarding gaps, not a reason to introduce elaborate governance before the project needs it.

### What is not a repository defect

Local virtual environments, build outputs, runtime databases, and the Hermes benchmark checkout are ignored. Their existence on this machine does not mean they are committed or shipped. Do not delete them simply to make the explorer look cleaner. Configure search/snapshot exclusions where needed.

### Recommended organization: incremental, not a rewrite

Keep the current broad layout and clarify ownership:

| Area | Intended responsibility |
| --- | --- |
| Root | One credible entry point: product status, installation, licensing, contributor/security pointers, build configuration. |
| Python package | Product services and adapters, with command/session behavior shared rather than copied across clients. |
| Rust crates | Surface-specific presentation and input; shared protocol types where appropriate. |
| Tests | Unit, cross-language contract, acceptance, and installed-artifact tests with explicit runners. |
| Documentation | Current user/contributor instructions and architecture decisions that still apply. |
| Audit/design history | Dated reference material, clearly separated from current support claims. |
| Skill catalogs | Explicit core/optional resource policy, provenance, and packaging tests. |
| Vendor | Pinned upstream provenance, required license material, update instructions, and deliberate shipped contents. |

First extractions should follow concrete change pressure: command dispatch, conversation lifecycle, configuration writes, and release-resource resolution. Do not reorganize everything before those behaviors have tests.

---

## 7. What to borrow from Hermes—and what not to copy

| Borrow | Why | Do not copy blindly |
| --- | --- | --- |
| Shared command metadata and capability visibility | Prevents help, clients, and dispatch from inventing different products. | Every command or platform-specific field in Hermes's registry. |
| Draft ownership, submission identity, failure restoration | Protects user work across asynchronous operations. | The entire React/Electron architecture or all accumulated state machinery. |
| Real-app, real-backend tests with a scripted model | Tests integration without paid API calls or nondeterministic model behavior. | A large visual snapshot suite before a small stable behavioral lane. |
| Configuration read/write ownership | Prevents settings corruption and inconsistent scope. | Complex profile/backend abstraction before HEX needs it. |
| Ratchets and recurring-bug guards | Stops debt growing while allowing incremental improvement. | Thousands of tests or arbitrary file-count targets. |
| Packaged-artifact first-chat checks | Makes shipping part of the engineering definition of done. | Hermes's entire custom package-management system. |
| Design rules about feedback and focus | Produces consistent interactions rather than matching colors. | Hermes branding or the assumption that matching its theme creates parity. |

Hermes's own core workflow explicitly describes an older visual lane as disabled for flakiness. Its code-health and docs guards baseline existing debt. Its root and large modules remain difficult to navigate. These are reasons to adopt the mechanisms selectively, not to idealize the reference project.

---

## 8. A prioritized completion roadmap

The order below is dependency-based, not a promised calendar estimate.

### Stage A — Restore truthful behavior and status

- Remove fabricated success and static operational telemetry from ACP commands.
- Mark desktop and unsupported native features experimental or unavailable.
- Correct installation commands, product-version identity, test badges, and unsupported comparative claims.
- Publish a small surface/capability table derived from actual dispatch.
- Fix Python test collection and make the 56 existing tests part of the gate.

**Exit condition:** No documented command silently simulates its promised effect; users can distinguish available functionality from planned functionality.

### Stage B — Finish one supported conversation surface

The native TUI is a reasonable candidate because it already launches a backend and runs an event loop. The Python console is also a valid temporary supported surface if native integration cannot be finished promptly. The choice should be driven by the acceptance results, not the newest framework.

- Define session identity, transcript persistence, and task association.
- Fix streaming/tool/terminal-event contracts.
- Implement active cancellation and authoritative completion state.
- Connect shared command operations rather than duplicate dispatcher logic.
- Run first-use setup through the actual default launch path.

**Exit condition:** Install → configure → send → tool execution → approval or failure → completion → follow-up → cancel → restart/resume works in one declared surface.

### Stage C — Make the release artifact the test subject

- Deliver the native binary or intentionally choose a Python-only default.
- Resolve vendor/skill resources without reliance on the checkout or current directory.
- Test supported Python/OS boundaries or narrow the support declaration.
- Lock release inputs; test from an isolated home outside the repository.
- Add upgrade tests that preserve settings and sessions.

**Exit condition:** A fresh installation completes the same supported journey as a developer checkout.

### Stage D — Finish desktop on the shared contract

- Connect GPUI startup, focus/input, bridge subscription, and actions.
- Reuse the established backend/session model.
- Add draft preservation, working transcript controls, and recovery states.
- Verify keyboard/accessibility behavior and actual packaged startup.
- Measure performance under a published workload before making comparative claims.

**Exit condition:** Desktop meets the same behavioral acceptance matrix, plus real input/focus and packaging checks.

### Stage E — Reduce maintenance friction continuously

- Extract broad dispatch/configuration responsibilities behind tests.
- Introduce a small static/code-health ratchet.
- Separate historical audits/design references from current product guidance.
- Establish vendor and bundled-resource ownership.
- Apply the visual design system after the interaction primitives work.

**Exit condition:** Adding a supported feature changes one behavior definition and its adapters/tests, rather than requiring several independent implementations and manual documentation reconciliation.

---

## 9. The acceptance matrix that would change how HEX feels

These are proposed release requirements, not existing passing tests.

| Journey | Required observable result |
| --- | --- |
| Fresh installation outside the repo | Documented launch works without a preexisting binary, compiler, or hidden checkout resource. |
| First run without credentials | Clear setup route; skipping is honest; no false readiness claim. |
| Invalid credentials or malformed config | Actionable error, retained draft/settings, no success summary. |
| Send and follow up | The second turn has the correct conversation context and identity. |
| Slow streaming with pauses | App remains busy until a real terminal event; text stays ordered and complete. |
| Tool run and failure | Correct tool identity and lifecycle; failure is visible rather than converted into success. |
| Approval round trip | Decision reaches the correct pending operation; denial prevents that operation. |
| Cancellation | Active work receives cancellation; UI and backend agree on the terminal state. |
| Reconnect during output | No lost tail, duplicate messages, stale spinner, or incorrect session reassignment. |
| Restart and resume | Durable transcript and supported session identity load correctly. |
| Failed message submission | Draft and attachments survive in the correct conversation. |
| Rollback, memory, and skill commands | Reported result is independently verified against actual state. |
| Desktop controls | Keyboard/input and click actions work through the real application entry point. |
| Upgrade | Settings and sessions survive; installed versions and resource paths remain coherent. |

A scripted local provider is appropriate for these tests. It makes delays, errors, tool calls, and reconnect windows deterministic without pretending the backend itself is unnecessary.

### A stricter definition of “done”

A feature is not complete merely because its class exists, its widget renders, or its method passes a unit test. For a supported product claim, require:

1. Discoverable only where supported.
2. Connected to a real backend operation.
3. Correct state transitions, including failure and cancellation where relevant.
4. No loss of user work.
5. Available in the shipped artifact.
6. Tested through the relevant user surface and contract boundary.
7. Accurately documented, with no unmeasured guarantees.

## Bottom line

**The repository does not need to look more like Hermes. HEX needs fewer seams where a promise turns into a stub, a local assumption, or an untested second implementation.**

The highest-return next milestone is not more model features, more panels, or a new folder tree. It is one trustworthy, installable, recoverable conversation workflow—then carrying that same behavior into the desktop. That would improve the product's perceived maturity more than another architecture milestone or parity report.
