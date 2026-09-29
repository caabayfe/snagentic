---
description: Generate and enrich ServiceNow documentation for an instance
---

Scope: $ARGUMENTS

1. `snagentic_instance_status`; use `snagentic_instance_fetch` and
   `snagentic_instance_integrate` if the mirror is stale.
2. Identify the requested audience, capability, process, or user task. Use
   `snagentic_instance_table`, `snagentic_instance_search`, and
   `snagentic_instance_refs` to gather evidence from the mirror. Read the referenced
   metadata and code; do not infer business purpose or user steps from names alone.
3. Create missing source with `snagentic_instance_docs` in `scaffold` mode, then edit only
   `instances/<name>/documentation/`. Use capability pages for purpose and benefit,
   process pages for trigger-to-outcome flows, and guides for role-specific instructions.
4. Add explicit evidence references for technical claims. Never include property values,
   credentials, personal data, business records, or unapproved screenshots.
5. Run `snagentic_instance_docs` in `build` mode, then `check` mode. Review unresolved
   evidence, affected-document fingerprints, overdue reviews, and the generated diagrams.
6. Show the authored and generated diffs for human review. Preview with
   `mkdocs serve -f instances/<name>/docs/mkdocs.yml`.
7. Use `migrate` mode only to preserve legacy narrative blocks during transition; do not
   continue authoring new content inside generated pages.
