---
name: repo_understanding
purpose: Inspect repository files and AST call graphs to summarize module architecture
description: Inspect repository files and AST call graphs to summarize module architecture
risk_class: R1
allowed_tools: [list_files, read_file, query_graphify]
trigger: repo_understanding
---
# Repository Understanding Workflow

1. Call `list_files` to enumerate source and test files in the target repository.
2. Call `query_graphify` to build the AST symbol index and inspect 1-hop and 2-hop call dependencies.
3. Produce a source-linked architectural summary without mutating any workspace files.
