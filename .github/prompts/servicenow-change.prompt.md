---
description: Make a reviewed ServiceNow change through the instance mirror and agent update sets
---

Goal: $ARGUMENTS

Follow this sequence and stop for user approval where marked.

1. Pick the instance with `snagentic_instance_list`; confirm it is `kind: development`.
2. `snagentic_instance_fetch`, then `snagentic_instance_status`. If the mirror is not
   integrated, run `snagentic_instance_integrate`, resolve any conflict markers, and commit.
3. Research the current design: `snagentic_instance_search` and `snagentic_instance_refs`
   for the tables, script includes, events and properties involved; read the files under
   `instances/<name>/metadata/<scope>/<class>/`. Summarize what exists and what will change.
4. Check `snagentic_instance_activity` and `snagentic_instance_collisions`. If someone
   else holds the same records in an open update set, tell the user before editing.
5. Design with the `servicenow-architect` skill, or delegate non-trivial designs to the
   `servicenow-architect` agent (configuration and low-code before script,
   no edits to out-of-box records when a new record will do). Make the smallest correct
   edits under `instances/<name>/metadata/` only, following the
   `servicenow-server-scripting`, `servicenow-client-ux`, `servicenow-security` and
   `servicenow-integrations` skills. Commit the edits on a feature branch; the branch
   name becomes the update set label.
6. Run `snagentic_instance_plan` and review it with the `servicenow-reviewer` skill, or
   delegate to the `servicenow-reviewer` agent, which records its verdict with
   `snagentic_instance_review_record`. Fix every `block` finding (the `gate` refuses
   apply otherwise; only a human may add a waiver) and fix or justify the others, citing
   the rule ID. Present every change, collision, remaining finding and the gate. **Stop
   and wait for explicit approval.**
7. On approval, call `snagentic_instance_apply` with `instance`, `planId`, `confirm: true`.
   Report the update sets used and anything that failed verification.
8. Run `snagentic_instance_scan` (ServiceNow Instance Scan of the agent update sets) and
   report its findings. Suggest ATF coverage (`snagentic_instance_ops_run` with
   `atf.run`) and, when the feature is complete, `snagentic_instance_promote`.
