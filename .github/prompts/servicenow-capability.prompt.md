---
description: Analyse how a ServiceNow instance behaves, then design and build a capability on it
---

Capability: $ARGUMENTS

The instance mirror holds every metadata record (out of box and customized) with its code,
plus a derived table model. Analyse from the mirror first; the instance is only touched to
fetch and to apply a reviewed plan.

1. **Refresh.** `snagentic_instance_list` (confirm `kind: development` before any write),
   `snagentic_instance_fetch`, `snagentic_instance_status`; integrate and commit if needed.
2. **Map the tables.** For each table involved call `snagentic_instance_table` (for example
   `incident`). It returns fields (types, references, choices), the inheritance chain and all
   behaviour that applies, own and inherited: business rules by `when`/`order`, client
   scripts, UI policies with their field actions, UI actions, ACLs with roles, notifications,
   events with script actions, SLAs, assignment rules, data policies and dictionary
   overrides. Each entry has a `path` under `instances/<name>/`; `customized: true` marks
   customer changes. The functional page `instances/<name>/docs/pages/tables/<table>.md`
   (after `snagentic_instance_docs`) shows the same behaviour in execution order.
3. **Read the code that matters.** Open the `script.js` (and `condition`) of rules in the
   phases you will affect: *before* rules that set the fields you touch, *after*/*async*
   rules that fire events, client scripts and UI policies on the same fields, ACLs on the
   operation. Follow calls with `snagentic_instance_refs` (script includes, events,
   properties, tables) and `snagentic_instance_search`. Flow Designer logic lives in
   `metadata/<scope>/sys_hub_flow/<flow>/_children/` (decoded, read only); form layouts in
   `sys_ui_section/<section>/_children/`.
4. **Check who else is working here.** `snagentic_instance_activity` and
   `snagentic_instance_collisions`; mention any open update set that holds the same records.
5. **Design.** Use the `servicenow-architect` skill or agent. Summarize current behaviour, the gap,
   and the smallest change: prefer configuration and low-code, and a new
   record (business rule, script include, UI policy, flow) over editing out-of-box records,
   which creates upgrade skips. State the execution phase and order you choose and why it
   does not conflict with existing rules. **Stop for user approval of the design.**
6. **Build.** Create a folder under `instances/<name>/metadata/<scope>/<class>/<slug>/`
   with `record.yaml` (fields, no `sys_id`) and code files such as `script.js`; copy field
   names from an existing record of the same class. Do not edit `_children/`, `_meta.yaml`
   or the `model/` folder. Commit on a feature branch; the branch name is the update set
   label.
7. **Plan, review and apply.** `snagentic_instance_plan`; review its `review` findings
   with the `servicenow-reviewer` skill or agent (fix every `block` finding, since the gate
   refuses apply otherwise; fix or justify the rest by rule ID); present each change,
   collision, remaining finding and the gate and **stop for explicit approval**. Then
   `snagentic_instance_apply` with `instance`,
   `planId` and `confirm: true`. Report the update set name and link
   (`<instance>/sys_update_set.do?sys_id=<id>`).
8. **Verify and document.** Run `snagentic_instance_scan` and report its findings.
   Suggest or run ATF (`snagentic_instance_ops_run`). Create or
   update the relevant capability, process, and guide sources under
   `instances/<name>/documentation/`; link every technical claim to mirrored evidence.
   Run documentation build/check and review the diagrams and affected-document findings.
   Leave the update set *in progress* for review unless the user asks to promote it.
