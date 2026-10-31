---
name: technical_research
purpose: Produce source-backed technical briefs from repository files and governed project memory
description: Produce source-backed technical briefs from repository files and governed project memory
risk_class: R1
allowed_tools: [list_files, read_file, query_graphify]
trigger: technical_research
---
# Source-Backed Technical Research Workflow

1. Retrieve relevant project-scoped facts and inspect primary repository source files via `read_file`.
2. Verify symbol relationships via `query_graphify`.
3. Synthesize findings with explicit file/line provenance and record a Proof-Carrying Artifact bundle.
