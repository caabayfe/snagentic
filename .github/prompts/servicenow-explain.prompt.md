---
description: Explain how a ServiceNow feature works using the local instance mirror
---

Question: $ARGUMENTS

1. Use `snagentic_instance_status` to confirm the mirror is recent; offer
   `snagentic_instance_fetch` if it is stale. Do not change anything.
2. Locate the entry points with `snagentic_instance_search` (names, tables, script text).
3. Follow the dependency chain with `snagentic_instance_refs`: table -> business rules,
   client scripts, ACLs, UI actions/policies; script include -> callers; events ->
   script actions and notifications; properties -> readers.
4. Read the relevant `record.yaml` and script files. Quote file paths so the user can open
   them.
5. If `instances/<name>/documentation/` contains related capability, process, or guide
   sources, use their reviewed purpose, benefits, ownership, and user instructions. Keep
   those claims distinct from facts inferred from metadata.
6. Answer with: purpose, trigger conditions, execution order (`when`, `order`), data
   touched, integrations, user impact, and risks. Include a Mermaid diagram when it helps.
7. Mention open update sets that currently change these records
   (`snagentic_instance_activity`).
