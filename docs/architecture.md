# Architecture

## Trust boundaries

`snagentic` separates four concerns:

1. ServiceNow remains authoritative for runtime execution and platform-generated
   metadata.
2. Git is authoritative for reviewed development code and normalized configuration.
3. The local `.snagentic/` directory holds disposable raw responses, locks, mirror
   work trees, search indexes, UI evidence, and promotion manifests.
4. ServiceNow roles on the integration user, plus the local `kind` policy, bound what
   the tool can change.

The filesystem is not a database backup. Mirrored metadata is a reviewed development
workspace and evidence source for agents.

## Environment flow

Development supports guarded two-way synchronization. Test and production reject direct
writes from both the local client policy and ServiceNow roles. Promotion uses immutable
application versions, update sets, or supported ServiceNow CI/CD APIs.

## Instance mirror

`snagentic instance ...` (package `snagentic.instance`) is the primary engine. It
mirrors whole ServiceNow instances into git so coding agents can read the design,
see who is changing what, generate documentation, and prepare changes.

### Layout

```
instances/<name>/
  instance.yaml                  # non-secret profile: url, kind, auth env names, sync, ui
  metadata/[domains/<domain>/]<scope>/<sys_class_name>/<slug>--<sys_id>/
    _meta.yaml                   # sys_id, class, scope, update name, mod count, hash
    record.yaml                  # normalized, redacted fields
    <field>.<ext>                # script/html/css/xml/json fields exploded to files
    _children/<table>.yaml       # read-only child rows: flow logic, form/list layout,
                                 # workflow activities, variable values (decoded)
  model/
    tables/<table>.yaml          # derived: fields, inheritance and all behaviour per table
    customer-updates.yaml        # every customized record, newest change first
  update-sets/<state>/<slug>--<sys_id>/
    update-set.yaml              # name, state, application, owner, dates
    changes.yaml                 # sys_update_xml entries (type, action, name, author) - no payloads
  docs/                          # generated MkDocs site source (mkdocs.yml + pages/)
.snagentic/<name>/               # local only: sync-state.json, catalog.json, index.sqlite,
                                 # mirror-tree/, mirror.index, mirror-index.json,
                                 # model-cache.json, children-owners.json,
                                 # pending-apply.json, ui/, promotions/
git branch servicenow-remote/<name>   # remote state only; never edited by hand
```

Any number of instances can live in one repository. Each gets its own folder,
mirror branch, and state directory. Only instances with `kind: development` can be
written.

### Reading: generic Table API sync

- **Coverage.** Every class that extends `sys_metadata` is discovered from
  `sys_db_object`; field types come from `sys_dictionary`. Agents need the platform's own
  behaviour, not only customizations, so `baseline_classes: ["*"]` (default) mirrors
  **every** metadata class in full, out-of-box records included: business rules, script
  includes, client scripts, UI actions/policies, ACLs, notifications, events, scheduled
  jobs, flows and actions, catalog items and variables, UI Builder, Service Portal,
  Virtual Agent, Now Assist skills, ATF, and more (about 2,200 classes on Australia).
  `baseline_exclude` lists *exact* classes that are too large or derivable to mirror
  record by record: `sys_dictionary` and `sys_documentation` (the schema and labels are in
  the table model; dictionary subclasses such as flow and catalog variables are kept),
  translations, ACL role links (listed on each ACL in the model) and compiled flow
  snapshots. Customized records of excluded classes are still mirrored: every tracked
  change writes a `sys_update_xml` row (into the Default update set when none is
  chosen), and the inventory reads those names. Replace `"*"` with a class list to
  narrow coverage; changing coverage settings forces a full fetch. A default denylist
  removes credential, certificate, translation and generated classes, and
  `redact_fields` removes values such as `sys_properties.value`. Exact reviewed,
  non-secret property names may be added to `sync.property_value_allowlist`; their values
  then participate in version control, change plans and optimistic conflict detection.
  Secret-like property names are rejected even when explicitly configured. Password-typed fields
  are always removed. Tables the integration user cannot read (some return 403 even for
  `admin`) are reported as `unreadable_tables` and their mirrored records are kept.
- **Operational inventory.** `operational_tables` mirrors `v_plugin`, `sys_plugins`,
  `sys_store_app`, and `domain` by default even though they are not reliably available
  through `sys_metadata`. Fetch performs a complete lightweight presence inventory for
  these tables on every run, downloads only new or changed rows, and removes records no
  longer present. Their `_meta.yaml` files are marked `read_only`; the planner reports
  local edits or deletions as ignored and never turns them into ServiceNow writes. If
  the supported Table API denies `sys_store_app`, the read-only
  `export-app-inventory` Playwright recipe traverses Application Manager, stores a
  normalized local snapshot under `.snagentic/<instance>/operational/`, and the next
  fetch mirrors that snapshot. The result reports each table's source, unreadable
  tables, and whether plugin, application-repository, and domain capabilities are
  complete; it never calls undocumented Application Manager endpoints or bypasses ACLs.
- **Child rows.** Flow Designer logic (`sys_hub_*_instance_v2`, stages), action steps,
  form and list layouts (`sys_ui_element`, `sys_ui_list_element`), legacy workflow
  versions, activities, transitions and conditions, and variable values are plain tables,
  not `sys_metadata`. `child_tables` maps each to its parent field; rows are written,
  read-only, to `_children/<table>.yaml` in the owning record's folder (nested owners
  such as `wf_activity` -> `wf_workflow_version` -> `wf_workflow` are resolved), and
  compressed values (gzip + base64, such as flow step inputs) are decoded to JSON. The
  planner ignores `_children`: change flows in Flow Designer or through their parent.
- **Full fetch** lists ids per class in parallel (`workers`, default 6) and downloads
  full rows only for new or changed records, so a coverage change or `--full` does not
  re-download the mirror. **Incremental fetch** runs one delta query on `sys_metadata`
  for all mirrored classes plus the customer-updates inventory.
- **Table model.** After each fetch, `model/tables/<table>.yaml` is derived from the
  cached `sys_db_object`, `sys_dictionary`, `sys_choice` and ACL roles (refreshed
  incrementally): fields with types, references and choices, the inheritance chain, and
  every business rule, client script, UI policy (with actions), UI action, ACL (with
  roles), notification, event (with script actions), data policy, dictionary override,
  SLA and assignment rule attached to the table, each linked to its mirrored folder and
  marked `customized` when it has customer updates. This is the agent's entry point.
- **Incremental fetch.** The inventory reads the changed `sys_update_xml` names, then
  `sys_id`, class, `sys_updated_on` and `sys_mod_count` for those records from
  `sys_metadata` (or all of `sys_metadata` with `include_baseline`), using keyset paging on
  `(sys_updated_on, sys_id)` with an overlap window (default 15 hours, which also
  absorbs time-zone interpretation of raw timestamps). The Table API removes
  ACL-denied rows *after* applying the limit, so paging ends only on an empty page,
  never on a short one. Only records whose mod count or
  update time changed are fetched, in batches per class.
- **Deletes** come from `sys_metadata_delete`, `sys_update_xml` DELETE entries, and a
  presence check of candidate ids. `fetch --full` diffs the complete inventory and is
  the safety net for anything missed.
- **Mirror commit.** Each fetch writes a private work tree (`.snagentic/<name>/mirror-tree`)
  and commits it to `servicenow-remote/<name>` with git plumbing (private index, no
  checkout), so the user's working tree and index are never touched. The commit message
  records counts, watermark, and authors.

### Hash contract (v1)

`sha256(canonical_json({"class": <sys_class_name>, "fields": {k: canonical_text(v)}}))`,
where `canonical_text` converts CRLF/CR to LF and strips trailing newlines, and
bookkeeping fields (`sys_updated_on`, `sys_mod_count`, etc.) are excluded. Exploded
files are written as `canonical_text + "\n"`. Shared vectors live in
`tests/fixtures/hash-vectors.json`.

### Integrating remote changes

`instance integrate` merges `servicenow-remote/<name>` into the current branch with
`git merge --no-ff` (the first merge uses `--allow-unrelated-histories`). Git's
three-way merge is the reconciliation engine: remote-only changes fast-forward,
converged changes merge cleanly, and conflicting hunks get standard conflict markers.
Integration refuses to run while `instances/<name>/metadata` or `update-sets` has
uncommitted changes, so local work is never discarded.

Test and production mirrors may be integrated into their own folders to read and
document them, and `git diff servicenow-remote/prod servicenow-remote/dev` shows
drift. Writing is restricted by `kind`, not by branch.

### Writing: update-set native

1. **Plan** (`instance plan`) diffs `instances/<name>/metadata` against the mirror
   tree. A new record is a new directory with `record.yaml` (and optional field files)
   and no `sys_id`. The plan has a stable `plan_id` and lists collisions with records
   held in other open update sets. Preconditions: the mirror exists, is integrated
   into `HEAD` (otherwise the plan would revert remote changes), and no conflict
   markers or unmerged files remain.
2. **Apply** (`instance apply --plan-id <id> --confirm`, development only):
   - blocks on collisions unless `--allow-collisions`;
   - per application scope, creates or reuses the update set
     `snagentic: <label> [<scope>]` (label defaults to the git branch);
   - makes it current for the integration user through `sys_user_preference`
     (`sys_update_set`, `updateSetForScope<id>`) and restores the previous value;
   - checks each record's current hash against the plan base (optimistic concurrency)
     before writing it through the Table API;
   - verifies that the writes were captured in `sys_update_xml`;
   - refetches the touched records and update sets, commits the mirror, and rewrites
     the touched local directories in canonical form. A `pending-apply.json` marker
     blocks further applies if the run is interrupted; the next fetch clears it.
3. **Promote** (`instance promote --confirm`) completes the agent update sets for the
   label and writes a content-free manifest (names, ids, change counts by type) to
   `.snagentic/<name>/promotions/`. Moving the completed sets to test/production stays
   with the supported deployment process (update source retrieval, pipelines).

### Update-set visibility

Open update sets, and sets changed within `update_set_window_days` (default 30), are
mirrored with their `sys_update_xml` entries but without payloads. `collisions` reports
records captured in more than one open set; `plan` reports local edits that touch
records held in someone else's open set; `activity` summarizes work by user,
application, and update set.

### Index and documentation

`instance index` builds `.snagentic/<name>/index.sqlite` (FTS5 when available) with
records and extracted references: tables (`collection`, `table`, `GlideRecord`),
script include calls, events (`gs.eventQueue`), and properties (`gs.getProperty`).
`instance docs` combines durable authored files under
`instances/<name>/documentation/` with facts derived from the mirror. Capability,
process, and guide Markdown files use validated YAML front matter for ownership,
audience, review status, cross-document links, process graphs, lifecycle states,
interactions, and technical evidence. The generator resolves evidence to table models
or mirrored records, renders audience-oriented pages and Mermaid diagrams, and writes a
fingerprint manifest used by `instance docs check` to identify stale output and affected
documents. Authored files and assets are never modified by a normal build.

The technical reference remains exhaustive: scope pages contain data models, fields,
business rules, client scripts, ACLs, UI actions/policies, scripted REST, flows,
scheduled jobs, property names, script-include API surfaces, and dependency flowcharts,
plus update-set and artifact-catalog pages. With the full out-of-box mirror, scope pages
detail customer changes and summarize the baseline by class. **Functional pages**
(`pages/tables/<table>.md`, for `docs.tables` and every table with customized behaviour)
explain behavior in execution order. Legacy narrative blocks remain readable and can be
copied to `documentation/legacy-narratives.yaml` with `instance docs migrate`.

The architecture deliberately separates facts from interpretation. Metadata and derived
models determine technical sections and evidence links. Human-reviewed source determines
business purpose, benefit, ownership, policy, and user instructions; the generator never
invents or silently rewrites those claims.

### Platform operations: API first, UI last

- `instance ops-run` wraps the supported CI/CD API (`/api/sn_cicd`): plugin
  activate/rollback, app install/rollback, update set create/retrieve/preview/commit/
  back out, ATF suite runs, and instance scans, polling `progress/{id}`. State-changing
  operations require a development instance and confirmation.
- `instance ui run <recipe>` runs a Playwright recipe (`ui/recipes/<name>/`) for gaps
  with no API: `login-check`, `upload-update-set` (XML import), and `activate-plugin`
  (for plugins that need the UI dialog). The runner logs in with a local (non-SSO)
  user whose credentials are passed only by environment variable name, starts tracing
  after login so passwords are never recorded, scrubs output, and stores screenshots,
  trace, and result under `.snagentic/<name>/ui/<run>/`. Mutating recipes require a
  development instance and confirmation; `--dry-run` shows the steps. Selectors are
  unverified until a recipe lists the releases it was tested on (`verified_releases`).

### Client surfaces: Copilot CLI extension and MCP server

The Python CLI (`src/snagentic/`) is the single engine described above; it is never
reimplemented client-side. Two thin, stateless front ends shell out to it over argv
(never a shell string) and share one tool catalogue so their behaviour is identical:

- **Copilot CLI extension** (`.github/extensions/snagentic/extension.mjs`) implements
  Copilot's proprietary extension API.
- **MCP server** (`.github/extensions/snagentic/mcp-server.mjs`, run via
  `snagentic mcp serve`) implements the standard [Model Context
  Protocol](https://modelcontextprotocol.io/) stdio transport, so any MCP-compatible
  client (not only Copilot CLI) can use the same tools.

Both import the tool catalogue (names, JSON Schemas, dispatch) from
`.github/extensions/snagentic/instance.mjs` as `instanceTools`, so a tool added once is
available identically from both surfaces: same inputs, same development-only/
`confirm: true` write gates, same underlying CLI invocation. Neither front end holds
business logic, authorization decisions, or direct ServiceNow access; they only
translate a client protocol call into a `snagentic instance ...` (or native-binary
equivalent) invocation and return its JSON result.

`snagentic mcp serve` resolves the same runtime each front end would otherwise need on
`PATH` (native binary, bundled Python venv, or Docker fallback) and forwards it
explicitly to the Node child process via `SNAGENTIC_EXECUTABLE`/`SNAGENTIC_PYTHON`,
unless the caller already set one of those or `SNAGENTIC_RUNTIME`. This matters because
an MCP client commonly launches `snagentic` by absolute path with a minimal inherited
environment, where `PATH` does not resolve `snagentic`; without forwarding, every tool
call would silently fall back to the Docker runtime instead of the native binary already
running the server. `snagentic mcp status` (and `doctor --local`'s `mcp` section) report
whether the Node runtime, `mcp-server.mjs`, and its `@modelcontextprotocol/sdk`
dependency are present and ready, without starting the server. Native and release
installs bundle the server's `node_modules`; only a source checkout needs
`npm install` in `.github/extensions/snagentic/`.

### Live instance findings

Verified against an Australia personal developer instance:

- **Basic auth restriction.** Since the 2026 basic-auth enforcement
  (`glide.authenticate.basic_auth.restriction.enforce`), REST basic auth is rejected
  with `401 User is not authenticated` unless the user has the
  `snc_basic_auth_api_access` role, even for `admin` with a valid UI password. Grant
  that role to the integration user on development instances, or use OAuth bearer
  tokens (`auth.mode: bearer`).
- Keyset paging with `^NQ` groups and `ORDERBY` on `sys_metadata` and `sys_update_xml`
  works; pages can be shorter than the limit because of ACLs.
- The Default update set (`is_default=true`) holds every change made without a chosen
  set. It is mirrored and reported, but it is not treated as holding records in
  collision checks.

### Assumptions to verify on a live instance

These are covered by the in-memory fake in `tests/instance/`, not yet by a real instance:

- Encoded-query comparison of raw `sys_updated_on` values in the integration user's
  time zone (mitigated by the overlap window; run the integration user in GMT).
- The `sys_metadata_delete` schema and retention.
- Table API writes being captured in the update set selected by the user preference,
  per scope, and cross-scope write restrictions for scoped applications.
- Table API honouring a client-supplied `sys_id` on insert.
- `javascript:gs.getUserID()` and `gs.daysAgoStart()` in REST encoded queries.
- Availability and roles for `sn_cicd` endpoints (`sn_cicd.sys_ci_automation`).
- Playwright selectors for classic pages and the Next Experience shell per release.

## Domain separation

Every mirrored metadata record carries explicit domain and scope identity in
`record.yaml` and its path under `instances/<name>/metadata/`. Ambiguous domains,
path traversal, implicit global fallback, and cross-domain moves are rejected.

## Data safety

The repository excludes credentials, encrypted values, tokens, journal fields,
business data, attachments, and unbounded logs. A troubleshooting bundle must be
explicitly sanitized before sharing.
