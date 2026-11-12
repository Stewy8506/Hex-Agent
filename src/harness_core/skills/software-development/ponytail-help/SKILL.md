---
name: ponytail-help
description: Use when listing ponytail commands.
version: 0.1.0
author: Stewy8506 (Stewy8506), Hermes Agent
license: MIT
platforms:
- linux
- macos
- windows
metadata:
  hermes:
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

Display this reference card when invoked. One-shot, do NOT change mode, write flag files, or persist anything.

## Levels
| Level | Trigger | Behavior |
|-------|---------|----------|
| **Lite** | `/ponytail lite` | Build what's asked, name the lazier alternative in one line. |
| **Full** | `/ponytail` | The ladder enforced: YAGNI → stdlib → native → one line → minimum. Default. |
| **Ultra** | `/ponytail ultra` | YAGNI extremist. Deletion before addition. Challenges requirements before building. |

Level sticks until changed or session end.

## Skills
| Skill | Trigger | What it does |
|-------|---------|--------------|
| `ponytail` | `/ponytail` | Lazy mode itself. Simplest solution that works. |
| `ponytail-review` | `/ponytail-review` | Over-engineering review: `L42: yagni: factory, one product. Inline.` |
| `ponytail-audit` | `/ponytail-audit` | Whole-repo over-engineering audit: ranked list of what to delete. |
| `ponytail-debt` | `/ponytail-debt` | Harvest `ponytail:` shortcut comments into a tracked ledger. |
| `ponytail-gain` | `/ponytail-gain` | Measured-impact scoreboard: less code, less cost, more speed. |
| `ponytail-help` | `/ponytail-help` | This card. |

## Deactivate
Say "stop ponytail" or "normal mode". Resume anytime with `/ponytail`. `/ponytail off` also works.

## Configure Default Mode
Default mode = `full`, auto-active every session. Change it:

- **Env var** (highest priority): `export PONYTAIL_DEFAULT_MODE=ultra`
- **Config file** (`~/.config/ponytail/config.json`): `{ "defaultMode": "lite" }`

Set `"off"` to disable auto-activation on session start. Resolution: env var > config file > `full`.

## More
Full docs + examples: https://github.com/DietrichGebert/ponytail
