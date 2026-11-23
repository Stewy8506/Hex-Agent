# Accessibility Statement

**HEX Agent** (`Stewy8506/Harness`) is committed to making autonomous software engineering tools usable for developers of all abilities across our CLI, terminal cockpit (`hex-tui`), native desktop app (`hex-desktop`), and local web dashboard.

## Supported Environments & Accessibility Features

- **CLI (`hex` / `harness`):** Plain-text stdout/stderr output compatible with screen readers, braille displays, and terminal accessibility tools; supports `NO_COLOR=1` and non-interactive flags for automation.
- **Native Terminal Cockpit (`hex-tui`):** Full keyboard-only navigation, command palette (`Ctrl+K` / `⌘K`), high-contrast Dark Zinc palette, and clear textual status badges (`[Completed]`, `[Failed]`, `[Pending]`) alongside color cues so status is never conveyed by color alone.
- **macOS GPUI Desktop (`App/hex-desktop`, experimental):** Keyboard-first workflow (`Enter` to send, `Shift+Enter` for newline, `⌘N` new session, `⌘K` command palette, `[y]`/`[a]`/`[n]`/`[l]` tool approval shortcuts) and high-contrast typography.
- **Voice Capability (`capabilities/voice.py`):** Optional hands-free spoken pair-programming mode.

## Known Limitations

- **Reduced Motion in `hex-desktop`:** GPUI `0.2` does not currently expose macOS `NSWorkspace.accessibilityDisplayShouldReduceMotion`, so the composer's 5-second breathing glow animation cannot automatically disable itself based on the OS preference yet. Tracking a manual user setting in `~/.hex/config.yaml` and an upstream GPUI upgrade.
- **Screen Reader Tree in `hex-desktop`:** As an experimental Metal/GPUI application, native macOS VoiceOver accessibility tree coverage is still incomplete. Users relying on VoiceOver are encouraged to use the CLI (`hex`) or `hex-tui` until GPUI accessibility bindings mature.

## Reporting Accessibility Barriers

If you encounter an accessibility barrier in any HEX interface, please open an issue on the `Stewy8506/Harness` repository with the `accessibility` label and include:

1. Which interface you are using (`hex` CLI, `hex-tui`, `hex-desktop`, or web dashboard)
2. Your operating system and assistive technology (e.g., VoiceOver, NVDA, high-contrast theme, keyboard-only)
3. What you were trying to do and where the barrier occurred

We prioritize accessibility fixes alongside core reliability work.
