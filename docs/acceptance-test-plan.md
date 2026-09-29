# snagentic Acceptance Test Plan

**Target instance:** `dev312411`  
**Instance kind:** development  
**Document purpose:** execution plan, acceptance criteria, evidence requirements, and
sign-off record for validating snagentic against a ServiceNow development instance.

## 1. Acceptance objective

The acceptance run must prove that snagentic can:

- load its Copilot CLI extension and expose local tools even when credential access is
  denied;
- synchronize and model ServiceNow metadata without losing local or remote changes;
- plan, review, apply, refetch, integrate, complete, back out, and reapply changes;
- reject stale plans, unapproved collisions, unsafe environments, and sensitive values;
- execute supported ServiceNow CI/CD operations;
- use Playwright only where a supported API is unavailable;
- retain reproducible, redacted evidence for every acceptance decision; and
- leave the development instance and Git repository in a known, supportable state.

This plan validates the product, its safety controls, and one representative business
feature. It does not authorize writes to test or production.

## 2. Acceptance decision

The release is accepted when:

1. every **Required** test passes;
2. every **Conditional** test passes when its prerequisite exists, or has an approved
   waiver with evidence that the prerequisite is unavailable;
3. no Critical or High defect remains open;
4. every mutating test has cleanup or an explicitly approved retained result;
5. the final instance status reports:
   - mirror integrated;
   - no pending apply;
   - no local metadata changes;
   - no unapproved collisions; and
   - an empty change plan;
6. all required evidence is present and contains no credentials, property values,
   journal content, or unrelated business data; and
7. the acceptance record is signed by the technical owner and business owner.

## 3. Evidence policy

### 3.1 Storage

Keep runtime evidence under the local-only directory:

```text
.snagentic/dev312411/acceptance/<run-id>/
```

Use a UTC run ID such as `20260924T160000Z`. The directory must not be committed.

Playwright continues to write its own evidence under:

```text
.snagentic/dev312411/ui/<timestamp>-<recipe>/
```

The acceptance record may reference those paths.

### 3.2 Required evidence per test

Each test record must contain:

- test ID and execution timestamp;
- Git commit and branch;
- instance name and instance kind;
- operator;
- approval reference for mutating operations;
- sanitized command/tool request;
- complete structured result or test-runner output;
- before/after instance status when the test mutates ServiceNow;
- created update-set IDs and states, when applicable;
- screenshots and `trace.zip` for UI tests;
- cleanup or rollback result; and
- final Pass, Fail, Blocked, or Waived decision with reviewer.

Do not copy credentials, system-property values, journal fields, attachments, or raw
diagnostics into the acceptance record.

### 3.3 Status definitions

| Status | Meaning |
|---|---|
| Pass | The measured result satisfies every acceptance criterion. |
| Fail | A criterion is not met or required evidence is missing. |
| Blocked | Execution cannot continue because an external prerequisite is unavailable. |
| Waived | A reviewer accepts a documented Blocked result for a Conditional test. |

## 4. Existing evidence baseline

Existing evidence demonstrates feasibility but does not replace a fresh formal
acceptance run unless the reviewers explicitly adopt it.

| Evidence ID | Result | Evidence |
|---|---|---|
| EV-BASE-001 | Ruff passed for the repository. | Session output from 2026-09-24 |
| EV-BASE-002 | Complete containerized Python test suite passed. | Session output from 2026-09-24 |
| EV-BASE-003 | All 29 extension and UI Node tests passed. | Session output from 2026-09-24 |
| EV-UI-001 | UI login and build detection passed on the Australia release. | `.snagentic/dev312411/ui/20260924T153104Z-login-check/` |
| EV-UI-002 | Incident Task proposal, relationship, prefill, save, and persisted-record verification passed. | `.snagentic/dev312411/ui/20260924T120247Z-incident-task-create/` |
| EV-UI-003 | Application Manager correctly reported HAM unavailable on developer instances. | `.snagentic/dev312411/ui/20260924T124352Z-install-app/` |
| EV-LIFE-001 | Incident Task update set completed, backed out with zero problems, reapplied, and completed. | `.snagentic/dev312411/promotions/2026-09-24T114953+0000.json` and `.snagentic/dev312411/promotions/2026-09-24T120309+0000.json` |
| EV-SYNC-001 | Final mirror was integrated with no pending apply, local metadata changes, or collisions. | Mirror commit `fb8370ae2ea656af9d5900a141ef73fb20d52675` |

## 5. Execution controls

### 5.1 Mandatory rules

- Run instance mutations only against an instance configured as `development`.
- Fetch and integrate before editing or planning.
- Inspect activity and collisions before every mutating phase.
- Show and approve every change plan before apply.
- Never use `allowCollisions` unless the collision test explicitly requires it and the
  collision has been approved.
- Prefer CI/CD API operations over UI recipes.
- Run every UI recipe in dry-run mode before the real execution.
- Do not create or modify repository `.env` files.
- Do not expose credentials in output or evidence.
- Commit or safely preserve intended metadata edits before fetch/integrate operations.
- Use unique acceptance labels and inactive disposable records.

### 5.2 Stop conditions

Stop the acceptance run when:

- the configured instance is not development;
- a collision involves another developer's update set;
- a plan contains an unexpected table, scope, domain, operation, or field;
- a remote hash changes without an intentional stale-plan test;
- evidence contains a credential or sensitive value;
- a cleanup or rollback operation fails;
- the instance becomes unavailable or reports an unsuccessful platform operation; or
- a test would require bypassing ServiceNow licensing or platform restrictions.

## 6. Test data and cleanup design

Use a unique run suffix in every disposable artifact:

```text
snagentic acceptance <run-id>
```

For metadata create/update/delete and collision tests, use an **inactive** disposable UI
Action with:

- a unique name and action name;
- `active: false`;
- `form_button: false`;
- `form_context_menu: false`;
- a false condition; and
- no executable business behavior.

The record must be deleted or backed out at the end of its test phase.

For business-record UI tests, use distinctive validation descriptions and retain only
the minimum identifiers required to prove persistence. Delete disposable records when
the platform and test design allow it.

## 7. Execution order

Run phases in order. A failed Required phase blocks later mutating phases.

1. Baseline and environment safety
2. Local regression suite
3. Read-only instance synchronization
4. Metadata create/update/delete lifecycle
5. Conflict, collision, and recovery controls
6. Selective system-property handling
7. CI/CD operations and rollback
8. Playwright workflows
9. Domain, performance, documentation, and promotion
10. Cleanup and sign-off

## 8. Detailed acceptance tests

### Phase A - Baseline and local regression

#### AT-001 - Repository quality gates

**Classification:** Required
**Approval:** None

**Execute**

```bash
docker compose run --rm -T --entrypoint ruff cli check .
docker compose run --rm -T --entrypoint pytest cli -q
node --test tests/extension tests/ui
```

**Pass criteria**

- All commands exit zero.
- No test is skipped unexpectedly.
- The Node run discovers all bundled recipes, including `install-app`.
- No credential value appears in output.

**Evidence**

- Complete command output.
- Exit codes.
- Git commit and worktree status.

#### AT-002 - Configuration and environment safety

**Classification:** Required  
**Approval:** None

**Execute**

1. Validate the instance configuration.
2. Confirm `dev312411` is `development` and writable.
3. Confirm configuration contains environment-variable names, not credential values.
4. Exercise the unit-tested production-write rejection and extension permission
   fallback as part of AT-001.

**Pass criteria**

- The instance is development and writable.
- No secret value exists in tracked configuration.
- Non-development writes remain denied.
- Credential permission denial does not remove local extension tools.

### Phase B - Read-only synchronization

#### AT-010 - Status, activity, and collision baseline

**Classification:** Required  
**Approval:** None

**Execute**

1. Read instance status.
2. List update sets and current activity.
3. Run collision reporting.
4. Record all pre-existing open agent and non-agent update sets.

**Pass criteria**

- Status is readable.
- Existing work is attributable.
- No unexplained collision affects a test record.
- The baseline is stored before any mutation.

#### AT-011 - Fetch, integrate, and no-op plan

**Classification:** Required  
**Approval:** None

**Execute**

1. Fetch the instance.
2. Integrate the mirror.
3. Resolve no conflicts unless the remote genuinely changed.
4. Generate a plan without editing metadata.

**Pass criteria**

- Fetch produces a mirror commit.
- Integration completes without unresolved markers.
- The no-op plan contains zero changes and zero collisions.
- Status reports integrated and no pending apply.

#### AT-012 - Index and table-model validation

**Classification:** Required  
**Approval:** None

**Execute**

1. Rebuild the local index.
2. Query known Incident and Incident Task metadata.
3. Generate the combined table view for `incident` and `incident_task`.
4. Follow at least one reference from each table to its mirrored code.

**Pass criteria**

- Index completes.
- Searches return expected records.
- Table output includes fields, inheritance, and applicable behavior.
- Every reported code path exists in the mirror.

### Phase C - Metadata lifecycle

#### AT-020 - Disposable create

**Classification:** Required  
**Approval:** Explicit plan/apply approval

**Execute**

1. Add the inactive disposable UI Action under `instances/dev312411/metadata/`.
2. Generate and review a plan.
3. Apply it under label `acceptance-crud-<run-id>`.
4. Integrate the canonical record returned by ServiceNow.

**Pass criteria**

- Plan contains exactly one `sys_ui_action` create.
- No sys_id exists before apply.
- No unexpected collision exists.
- Apply captures the record in an agent-owned update set.
- Refetch replaces the local record with a canonical sys_id-based path.
- A subsequent plan contains zero changes.

#### AT-021 - Disposable update

**Classification:** Required  
**Approval:** Explicit plan/apply approval

**Execute**

1. Change only the disposable record's description.
2. Plan, review, and apply the update.
3. Refetch and integrate.

**Pass criteria**

- Plan contains exactly one update and one expected field.
- The expected remote hash is present.
- ServiceNow increments modification metadata.
- A subsequent plan contains zero changes.

#### AT-022 - Disposable delete and restore

**Classification:** Required  
**Approval:** Explicit delete and rollback approval

**Execute**

1. Delete the disposable metadata directory.
2. Plan and verify exactly one delete.
3. Apply the delete.
4. Confirm the record is absent after refetch.
5. Back out or reapply as selected by the run owner, then return to the approved final
   state.

**Pass criteria**

- Delete is never inferred from an incomplete or stale mirror.
- Plan contains exactly one delete for the disposable sys_id.
- The remote record is absent after apply.
- Cleanup leaves no disposable active behavior.

### Phase D - Safety and concurrency

#### AT-030 - Stale-plan rejection

**Classification:** Required  
**Approval:** Explicit approval for the controlled remote change

**Execute**

1. Create a local update to the disposable record and generate a plan.
2. Change the same remote record through an independent approved update set.
3. Attempt to apply the original plan.
4. Fetch, integrate, and generate a replacement plan.

**Pass criteria**

- The original apply fails before writing because the remote hash changed.
- The failure names the affected path and requires fetch/integrate.
- No local intent is silently discarded.
- The replacement plan is based on the new remote state.

#### AT-031 - Collision rejection and approved override

**Classification:** Required  
**Approval:** Separate approval for the intentional collision and override

**Execute**

1. Capture the disposable record in open update set A.
2. Modify it locally and generate a second plan.
3. Verify the collision report identifies update set A, owner, action, and record.
4. Attempt apply without collision approval.
5. If reviewers approve, apply with `allowCollisions` to update set B.

**Pass criteria**

- Collision is visible before apply.
- Default apply is denied.
- Override is accepted only with explicit approval.
- Both update-set relationships are visible in evidence.
- Cleanup completes or backs out both disposable update sets.

#### AT-032 - Interrupted operation recovery

**Classification:** Required in test harness; Conditional live  
**Approval:** Live chaos-test approval

**Execute**

1. Retain the existing automated interruption/lock tests from AT-001.
2. For the optional live test, use only a disposable operation.
3. Terminate the dedicated runner process by its exact PID after the write starts.
4. Inspect locks, pending state, mirror state, and remote update-set capture.
5. Recover through status, fetch, integrate, and a new plan.

**Pass criteria**

- Automated recovery tests pass.
- A live run, when approved, does not duplicate a record or lose intent.
- Stale locks are detected and recoverable.
- The final plan is deterministic and no pending apply remains.

### Phase E - System-property safety

#### AT-040 - Default property redaction

**Classification:** Required  
**Approval:** None

**Execute**

1. Inspect a mirrored non-allowlisted `sys_properties` record.
2. Verify its value is absent/redacted.
3. Attempt a local value edit and generate a plan.

**Pass criteria**

- The value is not present in tracked metadata.
- Planning denies the unauthorized value change.
- The error instructs the operator to use the exact-name allowlist.

#### AT-041 - Allowlisted non-secret property round trip

**Classification:** Required  
**Approval:** Approval of the exact property name and test value

**Execute**

1. Use a dedicated harmless acceptance property.
2. Add only its exact name to `sync.property_value_allowlist`.
3. Perform a full fetch.
4. Verify value mirroring and hashing.
5. Update the value, plan, apply, refetch, and restore the original value.
6. Remove the property from the allowlist and fetch again.

**Pass criteria**

- Only the reviewed property value is visible.
- Secret-like property names remain rejected.
- The value participates in drift and optimistic-hash checks.
- The original value is restored.
- The value is redacted again after allowlist removal.
- No value appears in committed documentation or logs.

### Phase F - Platform operations

#### AT-050 - Update-set lifecycle

**Classification:** Required  
**Approval:** Explicit complete/back-out/reapply approval

**Execute**

1. Complete the agent-owned disposable update set.
2. Back it out through `update_set.back_out`.
3. Verify record removal or restoration.
4. Reapply the intended final state.
5. Complete the final update set.

**Pass criteria**

- Completion is restricted to agent-owned sets.
- Back-out reports successful with zero unresolved problems.
- The expected metadata state changes after back-out.
- Reapply restores the approved state.
- Promotion manifests contain metadata and hashes but no content or secrets.

#### AT-051 - Plugin activation and rollback

**Classification:** Required when a disposable developer-compatible plugin is approved  
**Approval:** Explicit activation and rollback approval

**Execute**

1. Select a low-risk plugin that is available on the developer instance.
2. Activate through `plugin.activate`.
3. Verify progress completion and resulting metadata.
4. Roll back through `plugin.rollback`.
5. If API activation requires an interactive dialog, use `activate-plugin` after a dry
   run.

**Pass criteria**

- API is preferred.
- Dependencies complete or are reported explicitly.
- Activation and rollback both reach successful terminal states.
- Final plugin state matches the approved final state.

#### AT-052 - Application installation success path

**Classification:** Conditional  
**Approval:** Explicit app and dependency approval

**Execute**

1. Select a free application explicitly available on developer instances.
2. Try `app.install` with repository sys_id, scope, and reviewed version.
3. If API delivery is unavailable, dry-run and execute `install-app`.
4. Review all dependency states before confirming.
5. Validate the installed scope and one non-destructive application workflow.
6. Roll back when the application is disposable.

**Pass criteria**

- The application is developer-instance compatible.
- Every dependency is named and approved.
- Installation reaches a successful terminal state.
- The scope and expected metadata appear after fetch.
- Rollback succeeds or the retained installation is explicitly accepted.

#### AT-053 - HAM platform-restriction negative path

**Classification:** Required negative test  
**Approval:** Installation attempt approval

**Execute**

1. Open Hardware Asset Management in Application Manager.
2. Start installation review.
3. Record the platform restriction and dependency statuses.

**Pass criteria**

- The recipe reports that HAM is unavailable on developer instances.
- The disabled Install action is not bypassed.
- Blocked dependencies are reported.
- No partial installation occurs.

**Existing evidence:** `EV-UI-003`

### Phase G - Playwright workflows

#### AT-060 - Login and release detection

**Classification:** Required  
**Approval:** Tool confirmation; no ServiceNow mutation

**Execute**

1. Run `login-check`.
2. Retain screenshot, result, and trace.

**Pass criteria**

- Login succeeds without exposing credentials.
- `stats.do` returns build name, tag, and date.
- Evidence is complete.

#### AT-061 - Incident Task positive workflow

**Classification:** Required  
**Approval:** Explicit record-creation approval

**Execute**

1. Dry-run `incident-task-create`.
2. Execute it with a unique validation description.
3. Verify proposed parent and inherited description.
4. Save, resolve the persisted record by number, and reopen it.

**Pass criteria**

- The action is visible on an active Incident.
- The parent Incident is correct.
- The source Incident is not submitted or modified.
- The description is prefilled and can be edited.
- Exactly one Incident Task is persisted.
- The reopened record contains the validation description.

#### AT-062 - Incident Task cancel, inactive, and authorization paths

**Classification:** Required  
**Approval:** None for cancel; approved test users for role checks

**Execute**

1. Open a proposal and navigate back without saving.
2. Open an inactive Incident.
3. Repeat with a user lacking required roles.
4. Verify the existing Workspace action independently.

**Pass criteria**

- Cancel creates no task.
- The classic action is absent on inactive Incidents.
- The action is absent for unauthorized users.
- Direct creation remains ACL-controlled.
- Workspace behavior is unchanged.

#### AT-063 - Update-set XML upload

**Classification:** Conditional  
**Approval:** Explicit import approval

**Execute**

1. Export a disposable, reviewed update set XML.
2. Dry-run `upload-update-set`.
3. Upload it through the UI recipe.
4. Verify it appears as a Retrieved Update Set.
5. Do not preview or commit unless separately approved.

**Pass criteria**

- File path remains repository-relative and `.xml`.
- Credentials and file contents are not logged.
- The retrieved set appears exactly once.
- No commit occurs without a separate approval.

#### AT-064 - UI failure evidence

**Classification:** Required  
**Approval:** None beyond the underlying attempted operation

**Execute**

1. Exercise one expected platform failure, such as HAM restriction.
2. Exercise one invalid recipe parameter in automated tests.

**Pass criteria**

- Failure is explicit and not success-shaped.
- `result.json` records the failing step.
- A failure screenshot and trace exist.
- Output does not expose credentials.

### Phase H - Advanced and operational validation

#### AT-070 - Domain-separated round trip

**Classification:** Conditional  
**Prerequisite:** Domain separation enabled and API user permitted to see a test domain

**Pass criteria**

- Domain records route under `metadata/domains/<stable-domain-id>/`.
- `sys_domain` and domain path survive plan/apply/refetch.
- Records outside API-user visibility are reported as an explicit coverage limitation.

#### AT-071 - Full-fetch and incremental performance

**Classification:** Required  
**Approval:** None

**Execute**

1. Measure one full fetch, index rebuild, and no-op plan.
2. Measure three incremental no-change fetches.
3. Record duration, result counts, and process exit status.

**Pass criteria**

- Full fetch completes within 30 minutes without process failure.
- Each incremental no-change fetch completes within 5 minutes.
- Incremental fetch does not rewrite unchanged records.
- Index and no-op plan complete after the fetch.
- Any threshold waiver includes measured baseline and justification.

#### AT-072 - Documentation regeneration

**Classification:** Required  
**Approval:** None

**Execute**

1. Regenerate instance documentation.
2. Compare generated pages.
3. Verify preserved narrative blocks.

**Pass criteria**

- Generated metadata sections match the final mirror.
- Narrative blocks remain unchanged.
- No property value, credential, or business record data enters documentation.

#### AT-073 - Promotion artifact safety

**Classification:** Required  
**Approval:** Complete-set approval

**Execute**

1. Generate a promotion manifest for the final accepted label.
2. Inspect it for schema, hashes, source instance, update-set IDs, and next steps.
3. Search it for values, scripts, secrets, credentials, journals, and business data.

**Pass criteria**

- Manifest is deterministic and content-free.
- It references only completed agent-owned update sets.
- It contains no deployment instruction to write directly to test or production.

#### AT-074 - Credential and diagnostics redaction

**Classification:** Required  
**Approval:** None

**Execute**

1. Run automated credential-scrubbing and environment-forwarding tests.
2. Run bounded diagnostics.
3. Review only the redacted diagnostics summary and local artifact paths.

**Pass criteria**

- Only approved environment-variable names are forwarded.
- No credential value appears in command arguments, output, evidence, or diagnostics.
- Diagnostics remain under `.snagentic/` and are not committed.

#### AT-075 - Operational inventory coverage

**Classification:** Required
**Approval:** Read-only UI confirmation when the supported Table API is ACL-blocked

**Execute**

1. Fetch the default operational tables.
2. When `sys_store_app` is unreadable, run the read-only `export-app-inventory`
   Application Manager recipe and fetch again.
3. Rebuild the index and search known plugin, application, and domain records.
4. Generate a final change plan and collision report.

**Pass criteria**

- `v_plugin`, `sys_store_app`, and `domain` each contain searchable records.
- Every operational record is marked read-only and cannot become a planned write.
- Table API and Application Manager snapshot sources are reported explicitly.
- Any remaining ACL-blocked table is reported without deleting prior mirror state.
- `required_capabilities_complete` is true.
- The final plan contains zero changes and the collision report contains zero collisions.

## 9. Final cleanup and verification

Execute after all mutating tests:

1. Remove or back out every disposable metadata and business record.
2. Complete or back out all acceptance-owned update sets as planned.
3. Fetch and integrate.
4. Rebuild the index if metadata coverage changed.
5. Generate a final no-op plan.
6. Run status, update-set, and collision reports.
7. Run AT-001 again.

**Final pass criteria**

- No disposable active behavior remains.
- No acceptance-owned update set remains unintentionally in progress.
- No unapproved collision remains.
- Mirror is integrated.
- No pending apply or local metadata change exists.
- Final plan contains zero changes and zero collisions.
- All local regression suites pass.

## 10. Traceability matrix

| Requirement | Tests |
|---|---|
| Extension availability and permission fallback | AT-001, AT-002 |
| Mirror correctness and discoverability | AT-010, AT-011, AT-012 |
| Create/update/delete planning and application | AT-020, AT-021, AT-022 |
| Drift and concurrency safety | AT-030, AT-031, AT-032 |
| Property-value confidentiality and controlled visibility | AT-040, AT-041 |
| Update-set completion, back-out, and promotion | AT-050, AT-073 |
| Plugin and application operations | AT-051, AT-052, AT-053 |
| UI authentication, positive workflows, and failures | AT-060 through AT-064 |
| Domain separation | AT-070 |
| Performance and operational readiness | AT-071, AT-072, AT-074, AT-075 |

## 11. Acceptance record

Complete one row per test during execution.

| Test ID | Status | Evidence path | Defect/waiver | Technical reviewer | Date |
|---|---|---|---|---|---|
| AT-001 |  |  |  |  |  |
| AT-002 |  |  |  |  |  |
| AT-010 |  |  |  |  |  |
| AT-011 |  |  |  |  |  |
| AT-012 |  |  |  |  |  |
| AT-020 |  |  |  |  |  |
| AT-021 |  |  |  |  |  |
| AT-022 |  |  |  |  |  |
| AT-030 |  |  |  |  |  |
| AT-031 |  |  |  |  |  |
| AT-032 |  |  |  |  |  |
| AT-040 |  |  |  |  |  |
| AT-041 |  |  |  |  |  |
| AT-050 |  |  |  |  |  |
| AT-051 |  |  |  |  |  |
| AT-052 |  |  |  |  |  |
| AT-053 |  |  |  |  |  |
| AT-060 |  |  |  |  |  |
| AT-061 |  |  |  |  |  |
| AT-062 |  |  |  |  |  |
| AT-063 |  |  |  |  |  |
| AT-064 |  |  |  |  |  |
| AT-070 |  |  |  |  |  |
| AT-071 |  |  |  |  |  |
| AT-072 |  |  |  |  |  |
| AT-073 |  |  |  |  |  |
| AT-074 |  |  |  |  |  |
| AT-075 |  |  |  |  |  |

## 12. Sign-off

| Role | Name | Decision | Date | Notes |
|---|---|---|---|---|
| Technical owner |  | Accept / Reject |  |  |
| ServiceNow platform owner |  | Accept / Reject |  |  |
| Business process owner |  | Accept / Reject |  |  |
