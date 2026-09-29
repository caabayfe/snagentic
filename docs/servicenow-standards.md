# ServiceNow standards: agents, skills, rules and gates

snagentic keeps Copilot's ServiceNow changes within good practice in layers. Prompts and
agents advise; tools enforce, so the result does not depend on which model proposed the
change.

1. **Skills** give the agent ServiceNow knowledge on demand.
   - `servicenow-architect`: choose configuration or low-code before script, and avoid
     customizing out-of-box records.
   - `servicenow-reviewer`: review a plan before approval.
   - `servicenow-server-scripting`, `servicenow-client-ux`, `servicenow-security` and
     `servicenow-integrations`: the coding standards.
2. **Agents** take the architect and reviewer roles (see
   [Copilot plugin](#copilot-plugin-agents-skills-and-hook)).
3. **Review rules** are deterministic checks in snagentic. Every rule has a stable ID,
   and the skills cite those IDs.
4. **The apply gate** refuses `apply` while unwaived `block` findings remain, or while a
   required review is missing. It runs in the Python CLI and again in a Copilot
   `preToolUse` hook.
5. **ServiceNow Instance Scan** gives the platform's own second opinion on the records
   in the agent update sets before promotion.

## Where findings appear

- **`snagentic_instance_plan` / `snagentic instance plan`**
  - The `review` section reports what the local change *introduces*. Findings already
    present in the mirrored version of an updated record are not repeated.
  - Findings do not affect `plan_id`.
  - The `gate` section says whether `apply` will be accepted (see below).
- **`snagentic_instance_review` / `snagentic instance review`**
  - With no selectors, it reviews the local changes, the same as plan.
  - `--all`, `--table`, `--scope`, `--path` and `--customized` scan mirrored records.
  - `--rule`, `--min-severity` and `--limit` filter the output.
  - `--rules` prints the rule catalogue.

```bash
snagentic instance -i dev review                        # local changes
snagentic instance -i dev review --customized           # customer-updated records only
snagentic instance -i dev review --table sys_script_client --min-severity warn
snagentic instance -i dev review --rules
```

Each finding has these fields:

- `rule`, `severity` (`block`, `warn` or `info`) and `category`.
- `path`, `field` and `line`.
- `message` and `fix`.
- `evidence`, which is the offending source line. It is redacted for credential rules.

## Rules

| Rule | Severity | Category | Check |
|------|----------|----------|-------|
| SN-SEC-001 | block | security | `eval`, `new Function`, `GlideEvaluator` |
| SN-SEC-002 | block | security | Literal passwords, tokens, keys or Authorization headers |
| SN-SEC-003 | warn | security | Encoded queries built by string concatenation |
| SN-SEC-004 | warn | security | ACL script that always evaluates to true |
| SN-SEC-005 | block | security | `Packages.*` Java calls |
| SN-PERF-001 | block | performance | `current.update()` in before/after business rules |
| SN-PERF-002 | warn | performance | New GlideRecord/GlideAggregate inside a loop |
| SN-PERF-003 | warn | performance | `getRowCount()` |
| SN-PERF-004 | warn | performance | `gs.sleep()` |
| SN-PERF-005 | block | performance | GlideRecord in client scripts |
| SN-PERF-006 | warn | performance | `getXMLWait()`, `getReference()` without a callback |
| SN-PERF-007 | warn | performance | REST/SOAP message in before/display business rules |
| SN-UPG-001 | block | upgradability | DOM, jQuery, `$()`, `gel()`, `document.` in client scripts |
| SN-UPG-002 | info | upgradability | The change customizes an out-of-box record |
| SN-MNT-001 | warn | manageability | Hardcoded 32-character sys_ids |
| SN-MNT-002 | warn | manageability | Hardcoded instance URLs |
| SN-MNT-003 | warn | manageability | `gs.log()`/`gs.print()` in scoped applications |
| SN-MNT-004 | warn | manageability | `setWorkflow(false)` |
| SN-MNT-005 | warn | manageability | Script include class name differs from the record name |
| SN-MNT-006 | info | manageability | New script record without a description |
| SN-UX-001 | warn | user experience | onChange client script without an `isLoading` guard |
| SN-UX-002 | info | user experience | Client script that only sets field state (use a UI policy) |
| SN-UX-003 | info | user experience | `alert()`/`confirm()` |

Scripts are analysed with an error-tolerant tokenizer, so matches inside comments and
strings do not count.

The script type is taken from the table and the record:

- Business-rule `when` and `advanced`.
- Whether a UI action is client-side.
- Whether the scope is global or a scoped application.
- Service Portal client controllers versus classic client scripts.

SN-UPG-002 uses `model/customer-updates.yaml` to recognise out-of-box records. It is
silent when the model has not been generated.

## Per-instance standards

Create `instances/<name>/standards.yaml` to adapt the rules to an instance:

```yaml
rules:
  SN-UX-002: {enabled: false}
  SN-PERF-003: {severity: info}
exclude_paths:
  - "metadata/global/sys_script_include/legacy-*"
```

snagentic rejects unknown rule IDs, keys and severities, so typos cannot silently
disable checks.

The same file configures the gate:

```yaml
gate:
  enforce: true          # false = report-only: findings are shown, apply is not refused
  require_review: false  # true = apply needs an approving review record for the plan
```

## Calibration

Calibration used the `dev312411` mirror: 72,740 records in reviewed tables, including
46,999 ACLs. A full scan takes about 4 minutes.

Of the 443 customer-updated records, 113 are in reviewed tables and produced 35
findings (24 SN-MNT-001, 8 SN-MNT-004, 2 SN-UPG-001, 1 SN-PERF-003). Out-of-box code
triggers these rules often too. That is why plan reports, and the gate blocks, only the
findings a change introduces. Tune severities per instance in `standards.yaml`, or start
with `gate: {enforce: false}` while calibrating.

Calibration also found false positives, which were fixed:

- Constant names such as `PAGE_TOKEN`.
- Property names stored in `*_PASSWORD` constants.
- ServiceNow namespace URIs such as `http://www.service-now.com/`.
- Namespaced `new sn_ws.RESTMessageV2`, which was previously missed.

## Apply gate

`plan` and `review` return a `gate`:

```json
{"mode": "enforcing", "passed": false, "counts": {"block": 1, "warn": 0, "info": 0},
 "blocking": [{"rule": "SN-PERF-001", "path": "instances/dev/metadata/...", "line": 2}],
 "waived": [], "expired_waivers": [], "require_review": false, "review": null,
 "reasons": ["1 blocking finding(s): SN-PERF-001 instances/dev/metadata/...:2"]}
```

When the gate is enforcing and does not pass, `apply` fails before any ServiceNow
request, and the message says what to do next. The gate passes when every `block`
finding the change introduces is fixed or waived, and, with `require_review`, when an
approving review record exists for the current `plan_id`.

### Waivers

Only a human writes waivers. They live in `instances/<name>/waivers.yaml`:

```yaml
waivers:
  - rule: SN-PERF-001
    path: metadata/global/sys_script/set-state--9c8df699cbc64ba98695c92013b839ca
    reason: Legacy rule; replaced by a flow in CHG0001.
    approver: jane.architect
    expires: 2026-12-31
```

The rules are strict:

- All five keys are required, and no others are allowed.
- `rule` must be a known rule ID.
- `path` is relative to the instance folder. It is a record folder or a glob such as
  `metadata/global/sys_script/legacy-*`. `*` on its own, `..` and absolute paths are
  rejected.
- `reason` must be at least 10 characters.
- `expires` must be a date at most 366 days away. Expired waivers stop applying and are
  listed in `expired_waivers`.

Waivers do not change `plan_id`, so adding one does not invalidate an approved plan.

### Review records

`snagentic_instance_review_record` (CLI: `snagentic instance -i <name> review-record
--plan-id ID --verdict approve|reject --reviewer NAME --notes TEXT`) writes
`instances/<name>/reviews/<plan_id>.yaml`. The record holds the verdict, the reviewer, a
timestamp, the notes, the finding counts and the waivers used.

- The plan ID must be the current plan. Any edit produces a new plan ID and needs a new
  review.
- `approve` is refused while unwaived `block` findings remain.
- Commit review records and waivers with the change; they are its audit trail.

## Copilot plugin: agents, skills and hook

The `copilot-plugin/` folder is a Copilot CLI plugin named `snagentic-servicenow`:

- `agents/servicenow-architect.agent.md` designs a change before anything is built. It
  has read-only tools and produces a design record: options (configure out of box, then
  low-code, then script), scope, and security, performance and upgrade impact.
- `agents/servicenow-reviewer.agent.md` reviews a plan: diff, findings, gate, collisions
  and Instance Scan results. It records its verdict with
  `snagentic_instance_review_record`. It cannot edit files, apply changes or write
  waivers.
- `skills/servicenow-*` are the six skills listed above.
- `hooks.json` registers a `preToolUse` hook for `snagentic_instance_apply`. The hook
  runs `snagentic copilot hook pre-tool-use`, which re-plans and denies the call when the
  plan ID is stale or the gate fails. It prints nothing otherwise, so Copilot's normal
  approval prompt still applies. Apply is also enforced in Python, so the gate holds even
  if the hook times out.

`snagentic copilot install` installs the plugin with the extension. It stages a local
marketplace in `$COPILOT_HOME/snagentic-marketplace`, points the hook at the absolute
`snagentic` executable, and registers the plugin with
`copilot plugin marketplace add` and `copilot plugin install
snagentic-servicenow@snagentic`. If `copilot` is not on `PATH`, it prints those commands
instead. `snagentic copilot status` reports whether the plugin is staged, current,
registered and enabled. `snagentic copilot uninstall` removes it. Use `--no-plugin` to
install only the extension.

In Copilot, the agents appear as `snagentic-servicenow:servicenow-architect` and
`snagentic-servicenow:servicenow-reviewer` (`/agent`). For plugin development, load the
checkout directly without installing:

```bash
copilot --plugin-dir ./copilot-plugin
```

## ServiceNow Instance Scan

After `apply`, ask ServiceNow to check the change with its own Instance Scan checks,
through the CI/CD API:

```bash
snagentic instance -i dev scan --confirm                   # point scan of agent update sets
snagentic instance -i dev scan --label my-branch --confirm
snagentic instance -i dev scan --target sys_script:<sys_id> --confirm
snagentic instance -i dev scan --suite <suite_sys_id> --confirm   # suite scan of the update sets
snagentic instance -i dev scan-results --label my-branch   # read-only, re-reads findings
```

The Copilot tools are `snagentic_instance_scan` (development instances only; asks for
confirmation) and `snagentic_instance_scan_results` (read-only).

- **Point scan** is the default. It scans each record captured in the agent update sets
  (`snagentic: <label> [...]`), with at most 50 records. It runs every active check that
  applies to the record's table.
- **Suite scan** (`--suite`) runs one scan suite over the update sets.
- Each run is followed to completion. Its `scan_result` findings are joined with their
  `scan_check` and returned with check name, category, priority (critical, high,
  moderate, low, planning), record, details, resolution and documentation link.
- Finding details have markup and record links removed, because instance-wide checks
  such as *Dormant User Account* put people's names in link text. Details are truncated.
  At most 200 findings are listed; the summary counts all of them, and
  `findings_truncated` says when the list was cut.
- The report is saved locally in `.snagentic/<name>/scans/<label>.json`. `promote` adds
  its summary to the promotion manifest, and recommends a scan first when none exists.

Scans create `scan_result` records on the instance, so they need a development instance
and `--confirm`.
