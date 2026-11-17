---
name: ponytail-debt
description: Use when listing deferred shortcuts.
version: 0.1.0
author: Stewy8506 (Stewy8506), HEX Agent
license: MIT
platforms:
- linux
- macos
- windows
metadata:
  hex:
    tags:
    - ponytail
    - yagni
    - minimalism
    - code-review
    related_skills:
    - simplify-code
    - requesting-code-review
    - plan
---

Every deliberate ponytail shortcut is marked with a `ponytail:` comment naming its ceiling and upgrade path. This collects them into one ledger so a deferral can't quietly become permanent.

## Scan
Grep the repo for comment markers, skipping `node_modules`, `.git`, and build output:

`grep -rnE '(#|//) ?ponytail:' .` (add other comment prefixes if your stack uses them)

Each hit is one ledger row. The comment prefix keeps prose that merely mentions the convention out of the ledger.

## Output
One row per marker, grouped by file:

`<path>:<line>: ceiling: <ceiling>. upgrade: <trigger>.`

The convention is `ponytail: <ceiling>, <trigger>`, so pull the ceiling and trigger straight from the comment. Want an owner per row too? `git blame -L<line>,<line> <path>`.

Flag the rot risk: any `ponytail:` comment that names no upgrade path or trigger gets a `no-trigger` tag — those are the ones that silently rot.

End with `<N> markers, <M> with no trigger.` Nothing found: `No ponytail: debt. Clean ledger.`

## Boundaries
Reads and reports only, changes nothing. To persist it, ask and it writes the ledger to a file (e.g. `PONYTAIL-DEBT.md`). One-shot. "stop ponytail-debt" or "normal mode" to revert.
