---
name: ponytail-audit
description: Use when auditing a repo for cuts.
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

ponytail-review, repo-wide. Scan the whole tree instead of a diff. Rank findings biggest cut first.

## Tags
Same as ponytail-review:
- `delete:` — dead code, unused flexibility, speculative feature.
- `stdlib:` — hand-rolled thing the standard library ships.
- `native:` — dependency or code doing what the platform already does.
- `yagni:` — abstraction with one implementation, config nobody sets.
- `shrink:` — same logic, fewer lines.

## Hunt
Deps the stdlib or platform already ships, single-implementation interfaces, factories with one product, wrappers that only delegate, files exporting one thing, dead flags and config, hand-rolled stdlib.

## Output
One line per finding, ranked: `<tag>: <what>. <replacement>. [<path>]`. End with `net: -<N> lines, -<M> deps possible.` Nothing to cut: `Lean already. Ship.`

## Boundaries
Scope: over-engineering and complexity only. Correctness, security, performance → route to normal review. Lists findings, applies nothing. One-shot. "stop ponytail-audit" or "normal mode" to revert.
