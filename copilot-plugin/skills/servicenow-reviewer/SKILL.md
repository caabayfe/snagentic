---
name: servicenow-reviewer
description: Review planned ServiceNow metadata changes against ServiceNow best practices (security, performance, upgradability, manageability, user experience) before apply. Use after editing instances/<name>/metadata and before asking the user to approve snagentic_instance_apply, or when asked to review ServiceNow code.
---

# ServiceNow reviewer

You review; you do not rewrite silently. Every finding cites a rule ID and a path.

## Procedure

1. `snagentic_instance_plan` (or `snagentic_instance_review` with no selectors). The
   `review` section lists findings the change **introduces**; pre-existing issues in the
   same record are not repeated. `snagentic_instance_review` with `listRules: true`
   returns the rule catalogue.
2. For each finding: read the code at `path`/`field`/`line`, confirm it is real, and
   either fix it (preferred) or explain why it is acceptable here.
3. Then review what rules cannot see, using the checklist below.
4. Report using the format at the end. Re-run plan after fixes; the `plan_id` changes.

`block` findings **stop apply**: `snagentic_instance_apply` (and the plugin's
`preToolUse` hook in front of it) refuses the plan until each one is fixed or covered by
an active waiver in `instances/<name>/waivers.yaml` (rule, record path glob, reason,
approver, expiry; at most one year). Only a human may approve a waiver; never write one
yourself. `warn` means "fix unless justified", `info` is advice. The plan's `gate`
section shows `blocking`, `waived` and `expired_waivers`.

When the review is done, record it with `snagentic_instance_review_record` (verdict
`approve` or `reject`, notes summarising the report). The record is bound to the
`plan_id`; instances with `gate: {require_review: true}` in `standards.yaml` refuse apply
without an approving record for the exact plan.

## Rule catalogue (enforced by snagentic)

| Rule | Severity | Check |
|------|----------|-------|
| SN-SEC-001 | block | eval / new Function / GlideEvaluator |
| SN-SEC-002 | block | credentials or Authorization headers in scripts |
| SN-SEC-003 | warn | encoded queries built by concatenation |
| SN-SEC-004 | warn | ACL script that is always true |
| SN-SEC-005 | block | `Packages.*` Java calls |
| SN-PERF-001 | block | `current.update()` in before/after business rules |
| SN-PERF-002 | warn | GlideRecord/GlideAggregate created inside loops (N+1) |
| SN-PERF-003 | warn | `getRowCount()` |
| SN-PERF-004 | warn | `gs.sleep()` |
| SN-PERF-005 | block | GlideRecord in client scripts |
| SN-PERF-006 | warn | `getXMLWait()`, `getReference()` without callback |
| SN-PERF-007 | warn | REST/SOAP calls in before/display business rules |
| SN-UPG-001 | block | DOM/jQuery/`gel()`/`document.` in client scripts |
| SN-UPG-002 | info | change customizes an out-of-box record |
| SN-MNT-001 | warn | hardcoded sys_ids |
| SN-MNT-002 | warn | hardcoded instance URLs |
| SN-MNT-003 | warn | `gs.log`/`gs.print` in scoped apps |
| SN-MNT-004 | warn | `setWorkflow(false)` |
| SN-MNT-005 | warn | script include name differs from its class |
| SN-MNT-006 | info | new script record without description |
| SN-UX-001 | warn | onChange without `isLoading` guard |
| SN-UX-002 | info | client script that only sets field state (use a UI policy) |
| SN-UX-003 | info | `alert()`/`confirm()` |

An instance can disable rules or change severities in `instances/<name>/standards.yaml`.

## Manual checklist (not machine-checked)

- **Design**: was a declarative option (UI policy, data policy, flow, configuration)
  available? Is the logic in a reusable script include? Right scope?
- **Security**: new tables and new client-callable script includes have ACLs /
  `isPublic()` false and role checks; GlideRecordSecure or `canRead()` where user data
  is returned; no user input rendered unescaped in Jelly/portal templates; no sensitive
  fields logged.
- **Performance**: business rule conditions set (not just `if` in the script);
  queries filter on indexed fields and use `setLimit`; display rules only fill
  `g_scratchpad`; no work in `query` rules that queries the same table.
- **Data**: choice values not labels; reference fields not strings; dates via
  GlideDateTime; no deletes without a condition.
- **Maintainability**: meaningful names, description filled, no dead code, messages via
  `gs.getMessage` for translation, consistent order values.
- **Testability**: an ATF test or clear manual test steps exist.

## Report format

```
Verdict: ready | changes needed | needs user decision
Plan: <plan_id>

Findings
- [SN-PERF-001][block] <path>:<line> — <what, why> → <fix / applied fix>
- [manual][warn] <path> — <what, why> → <fix>

Accepted with justification
- [SN-MNT-001] <path> — <reason the user accepted>
```
