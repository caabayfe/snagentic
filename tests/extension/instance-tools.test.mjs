import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { chmod, mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  buildInstanceArguments,
  buildUiLauncher,
  discoverInstanceCredentialNames,
  executeInstanceCommand,
  sanitizeInstanceArguments,
} from "../../.github/extensions/snagentic/instance.mjs";
import {
  applyRuntimeFile,
  validateCredentialNames,
} from "../../.github/extensions/snagentic/lib.mjs";

function fakeSpawn(responses, calls) {
  return (executable, argv, options) => {
    calls.push({ executable, argv, options, stdin: "" });
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    child.stdout.setEncoding = () => {};
    child.stderr.setEncoding = () => {};
    child.stdin = {
      on() {},
      end(text) {
        calls.at(-1).stdin = text;
      },
    };
    child.kill = () => {};
    const response = responses.shift();
    setImmediate(() => {
      child.stdout.emit("data", JSON.stringify(response.stdout ?? {}));
      if (response.stderr) child.stderr.emit("data", JSON.stringify(response.stderr));
      child.emit("close", response.code ?? 0);
    });
    return child;
  };
}

const devProfile = async () => ({
  instance: "dev",
  kind: "development",
  credentialNames: ["SNAGENTIC_DEV_USERNAME", "SNAGENTIC_DEV_PASSWORD"],
});
const prodProfile = async () => ({
  instance: "prod",
  kind: "production",
  credentialNames: ["SNAGENTIC_PROD_TOKEN"],
});

test("builds instance argv without shell interpolation", () => {
  const args = sanitizeInstanceArguments("search", { instance: "dev", text: "GlideRecord('x')" });
  assert.deepEqual(buildInstanceArguments("search", args), [
    "--json", "instance", "-i", "dev", "search", "GlideRecord('x')", "--limit", "50",
  ]);
  const apply = sanitizeInstanceArguments("apply", {
    instance: "dev", planId: "0123456789abcdef", confirm: true, allowCollisions: true,
  });
  assert.deepEqual(buildInstanceArguments("apply", apply).slice(4), [
    "apply", "--plan-id", "0123456789abcdef", "--confirm", "--allow-collisions",
  ]);
  const ops = sanitizeInstanceArguments("ops-run", {
    instance: "dev", operation: "plugin.activate", params: { plugin_id: "com.x" }, confirm: true,
  });
  assert.deepEqual(buildInstanceArguments("ops-run", ops).slice(4), [
    "ops-run", "plugin.activate", "--param", "plugin_id=com.x", "--confirm",
  ]);
});

test("builds the table model command", () => {
  const args = sanitizeInstanceArguments("table", { instance: "dev", table: "incident" });
  assert.deepEqual(buildInstanceArguments("table", args).slice(4), ["table", "incident"]);
  const own = sanitizeInstanceArguments("table", { table: "incident", inherited: false });
  assert.deepEqual(buildInstanceArguments("table", own).slice(2), [
    "table", "incident", "--no-inherited",
  ]);
  assert.throws(() => sanitizeInstanceArguments("table", { table: "../incident" }), /table/);
});

test("builds and validates documentation commands", () => {
  const defaults = sanitizeInstanceArguments("docs", { instance: "dev" });
  assert.deepEqual(buildInstanceArguments("docs", defaults).slice(4), ["docs", "build"]);

  const check = sanitizeInstanceArguments("docs", {
    instance: "dev", mode: "check", strict: true,
  });
  assert.deepEqual(buildInstanceArguments("docs", check).slice(4), [
    "docs", "check", "--strict",
  ]);

  const scaffold = sanitizeInstanceArguments("docs", {
    instance: "dev", mode: "scaffold", type: "process", id: "incident-resolution",
  });
  assert.deepEqual(buildInstanceArguments("docs", scaffold).slice(4), [
    "docs", "scaffold", "--type", "process", "--id", "incident-resolution",
  ]);

  assert.throws(
    () => sanitizeInstanceArguments("docs", { mode: "scaffold", type: "process" }),
    /id/,
  );
  assert.throws(
    () => sanitizeInstanceArguments("docs", { mode: "build", id: "not-allowed" }),
    /only supported/,
  );
});

test("rejects unsafe or unconfirmed instance arguments", () => {
  assert.throws(() => sanitizeInstanceArguments("status", { instance: "../x" }), /instance/);
  assert.throws(() => sanitizeInstanceArguments("status", { extra: 1 }), /unsupported/);
  assert.throws(() => sanitizeInstanceArguments("search", { text: "--full" }), /unsupported/);
  assert.throws(
    () => sanitizeInstanceArguments("apply", { instance: "dev", planId: "0123456789abcdef" }),
    /confirm=true/,
  );
  assert.throws(
    () => sanitizeInstanceArguments("apply", { planId: "0123456789abcdef", confirm: true }),
    /explicit instance/,
  );
  assert.throws(
    () => sanitizeInstanceArguments("ops-run", {
      instance: "dev", operation: "plugin.activate", params: { plugin_id: "a;rm -rf" }, confirm: true,
    }),
    /unsupported characters/,
  );
  assert.doesNotThrow(() =>
    sanitizeInstanceArguments("ops-run", { operation: "progress", params: { progress_id: "p1" } }),
  );
  assert.doesNotThrow(() =>
    sanitizeInstanceArguments("ui-run", { instance: "dev", recipe: "login-check", dryRun: true }),
  );
  assert.doesNotThrow(() =>
    sanitizeInstanceArguments("ui-run", { instance: "dev", recipe: "export-app-inventory" }),
  );
  assert.throws(
    () => sanitizeInstanceArguments("ui-run", { instance: "dev", recipe: "login-check" }),
    /confirm=true/,
  );
});

test("write commands are denied for non-development instances", async () => {
  const calls = [];
  const outcome = await executeInstanceCommand(
    "apply",
    { instance: "prod", planId: "0123456789abcdef", confirm: true },
    { inspect: prodProfile, spawnImpl: fakeSpawn([], calls) },
  );
  assert.equal(outcome.resultType, "denied");
  assert.equal(calls.length, 0);
});

test("read commands run without inspecting credentials", async () => {
  const calls = [];
  const outcome = await executeInstanceCommand("status", { instance: "dev" }, {
    inspect: async () => assert.fail("status must not inspect credentials"),
    spawnImpl: fakeSpawn([{ stdout: { ok: true, result: { integrated: true } } }], calls),
    sourceEnvironment: { PATH: "/usr/bin", SNAGENTIC_DEV_PASSWORD: "value" },
  });
  assert.equal(outcome.resultType, "success");
  assert.equal(calls[0].executable, "docker");
  assert.ok(!("SNAGENTIC_DEV_PASSWORD" in calls[0].options.env));
  assert.deepEqual(calls[0].argv.slice(-4), ["instance", "-i", "dev", "status"]);
});

test("ui-run prepares in the cli container and executes in the ui container", async () => {
  const calls = [];
  const request = {
    recipe: "login-check",
    username_env: "SNAGENTIC_DEV_USERNAME",
    password_env: "SNAGENTIC_DEV_PASSWORD",
  };
  const outcome = await executeInstanceCommand(
    "ui-run",
    { instance: "dev", recipe: "login-check", confirm: true },
    {
      inspect: devProfile,
      sourceEnvironment: {
        PATH: "/usr/bin",
        SNAGENTIC_DEV_USERNAME: "agent",
        SNAGENTIC_DEV_PASSWORD: "secret-value",
      },
      spawnImpl: fakeSpawn(
        [
          { stdout: { ok: true, result: { status: "prepared", request } } },
          { stdout: { ok: true, result: { logged_in: true } } },
        ],
        calls,
      ),
    },
  );
  assert.equal(outcome.resultType, "success", outcome.textResultForLlm);
  assert.ok(calls[0].argv.includes("--prepare-only"));
  assert.deepEqual(calls[1].argv, [
    "compose", "run", "--rm", "-T",
    "-e", "SNAGENTIC_DEV_USERNAME", "-e", "SNAGENTIC_DEV_PASSWORD", "ui",
  ]);
  assert.equal(JSON.parse(calls[1].stdin).recipe, "login-check");
  for (const call of calls) {
    assert.ok(!call.argv.join(" ").includes("secret-value"));
    assert.ok(!call.stdin.includes("secret-value"));
  }
});

test("instance credential names follow the reviewed convention only", async () => {
  assert.deepEqual(validateCredentialNames(["SNAGENTIC_ACME_DEV_PASSWORD"]), [
    "SNAGENTIC_ACME_DEV_PASSWORD",
  ]);
  assert.throws(() => validateCredentialNames(["AWS_SECRET_ACCESS_KEY"]), /unapproved/);
  assert.throws(() => validateCredentialNames(["SNAGENTIC_ACME_SECRET"]), /unapproved/);
  const root = await mkdtemp(path.join(os.tmpdir(), "snagentic-ext-"));
  try {
    await mkdir(path.join(root, "instances", "acme-dev"), { recursive: true });
    await writeFile(
      path.join(root, "instances", "acme-dev", "instance.yaml"),
      "name: acme-dev\nauth:\n  mode: basic\n  username_env: SNAGENTIC_ACME_DEV_USERNAME\n" +
        "  password_env: GITHUB_TOKEN\n",
    );
    assert.deepEqual(discoverInstanceCredentialNames(root), ["SNAGENTIC_ACME_DEV_USERNAME"]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
  assert.throws(() => buildUiLauncher(["GITHUB_TOKEN"]), /unapproved/);
});

test("remote commands load allowlisted credentials from the per-instance env file", async () => {
  const home = await mkdtemp(path.join(os.tmpdir(), "snag-home-"));
  try {
    await mkdir(path.join(home, ".config", "snagentic"), { recursive: true });
    const credentialFile = path.join(home, ".config", "snagentic", "dev.env");
    await writeFile(
      credentialFile,
      [
        "export SNAGENTIC_DEV_USERNAME=agent",
        "SNAGENTIC_DEV_PASSWORD='file-secret'",
        "AWS_SECRET_ACCESS_KEY=must-not-leak",
        "",
      ].join("\n"),
    );
    await chmod(credentialFile, 0o600);
    const calls = [];
    const outcome = await executeInstanceCommand("fetch", { instance: "dev" }, {
      inspect: devProfile,
      sourceEnvironment: { PATH: "/usr/bin", HOME: home, SNAGENTIC_DEV_USERNAME: "from-env" },
      spawnImpl: fakeSpawn([{ stdout: { ok: true, result: {} } }], calls),
    });
    assert.equal(outcome.resultType, "success", outcome.textResultForLlm);
    const env = calls[0].options.env;
    assert.equal(env.SNAGENTIC_DEV_USERNAME, "from-env");
    assert.equal(env.SNAGENTIC_DEV_PASSWORD, "file-secret");
    assert.ok(!("AWS_SECRET_ACCESS_KEY" in env));
    assert.ok(!calls[0].argv.join(" ").includes("file-secret"));
    assert.ok(calls[0].argv.includes("SNAGENTIC_DEV_PASSWORD"));
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("remote commands reject credential files accessible by other users", async () => {
  if (process.platform === "win32") return;
  const home = await mkdtemp(path.join(os.tmpdir(), "snag-home-"));
  try {
    const directory = path.join(home, ".config", "snagentic");
    await mkdir(directory, { recursive: true });
    const credentialFile = path.join(directory, "dev.env");
    await writeFile(credentialFile, "SNAGENTIC_DEV_PASSWORD=file-secret\n");
    await chmod(credentialFile, 0o644);

    await assert.rejects(
      executeInstanceCommand("fetch", { instance: "dev" }, {
        inspect: devProfile,
        sourceEnvironment: { PATH: "/usr/bin", HOME: home },
        spawnImpl: fakeSpawn([], []),
      }),
      /must not be accessible by group or others/u,
    );
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("runtime.env supplies only SNAGENTIC_PYTHON and never overrides the environment", async () => {
  const home = await mkdtemp(path.join(os.tmpdir(), "snag-home-"));
  try {
    await mkdir(path.join(home, ".config", "snagentic"), { recursive: true });
    await writeFile(
      path.join(home, ".config", "snagentic", "runtime.env"),
      'export SNAGENTIC_PYTHON="/opt/venv/bin/python"\nSNAGENTIC_DEV_PASSWORD=x\n',
    );
    await chmod(path.join(home, ".config", "snagentic", "runtime.env"), 0o600);
    const fromFile = applyRuntimeFile({ HOME: home });
    assert.equal(fromFile.SNAGENTIC_PYTHON, "/opt/venv/bin/python");
    assert.ok(!("SNAGENTIC_DEV_PASSWORD" in fromFile));
    const explicit = applyRuntimeFile({ HOME: home, SNAGENTIC_PYTHON: "/usr/bin/python3" });
    assert.equal(explicit.SNAGENTIC_PYTHON, "/usr/bin/python3");
    assert.equal(applyRuntimeFile({ HOME: path.join(home, "none") }).SNAGENTIC_PYTHON, undefined);
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("runtime.env rejects files accessible by other users", async () => {
  if (process.platform === "win32") return;
  const home = await mkdtemp(path.join(os.tmpdir(), "snag-home-"));
  try {
    const directory = path.join(home, ".config", "snagentic");
    await mkdir(directory, { recursive: true });
    const runtimeFile = path.join(directory, "runtime.env");
    await writeFile(runtimeFile, "SNAGENTIC_PYTHON=/opt/venv/bin/python\n");
    await chmod(runtimeFile, 0o644);

    assert.throws(
      () => applyRuntimeFile({ HOME: home }),
      /must not be accessible by group or others/u,
    );
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("builds and validates the review command", () => {
  const changes = sanitizeInstanceArguments("review", { instance: "dev" });
  assert.deepEqual(buildInstanceArguments("review", changes).slice(4), ["review"]);
  const scan = sanitizeInstanceArguments("review", {
    all: true, table: "sys_script", scope: "global", customized: true,
    path: "instances/dev/metadata/global/sys_script",
    rules: ["SN-PERF-001", "SN-SEC-002"], minSeverity: "warn", limit: 50,
  });
  assert.deepEqual(buildInstanceArguments("review", scan).slice(2), [
    "review", "--all", "--table", "sys_script", "--scope", "global",
    "--path", "instances/dev/metadata/global/sys_script", "--customized",
    "--rule", "SN-PERF-001", "--rule", "SN-SEC-002", "--min-severity", "warn", "--limit", "50",
  ]);
  const catalogue = sanitizeInstanceArguments("review", { listRules: true });
  assert.deepEqual(buildInstanceArguments("review", catalogue).slice(2), ["review", "--rules"]);
  for (const bad of [
    { path: "../secrets" },
    { path: "instances/dev/../../etc" },
    { path: "--all" },
    { rules: ["SN-PERF-1"] },
    { rules: "SN-PERF-001" },
    { minSeverity: "fatal" },
    { limit: 0 },
    { table: "Incident; rm" },
    { fix: true },
  ]) {
    assert.throws(() => sanitizeInstanceArguments("review", bad), TypeError, JSON.stringify(bad));
  }
});

test("builds and validates review records", () => {
  const record = sanitizeInstanceArguments("review-record", {
    instance: "dev", planId: "0123456789abcdef", verdict: "approve",
    reviewer: "servicenow-reviewer agent", notes: "-- starts with dashes; gate passed",
  });
  assert.deepEqual(buildInstanceArguments("review-record", record).slice(4), [
    "review-record", "--plan-id", "0123456789abcdef", "--verdict", "approve",
    "--reviewer=servicenow-reviewer agent", "--notes=-- starts with dashes; gate passed",
  ]);
  const base = { planId: "0123456789abcdef", verdict: "reject", reviewer: "r", notes: "0123456789" };
  for (const bad of [
    { ...base, planId: "XYZ" },
    { ...base, verdict: "maybe" },
    { ...base, reviewer: "a;b" },
    { ...base, notes: "short" },
    { ...base, notes: "x".repeat(4001) },
    { ...base, extra: true },
  ]) {
    assert.throws(() => sanitizeInstanceArguments("review-record", bad), TypeError,
      JSON.stringify(bad));
  }
});

test("builds and validates Instance Scan commands", () => {
  const id = "a".repeat(32);
  const scan = sanitizeInstanceArguments("scan", {
    instance: "dev", label: "feature-1", updateSets: [id], targets: [`sys_script:${id}`],
    suite: "b".repeat(32), confirm: true,
  });
  assert.deepEqual(buildInstanceArguments("scan", scan).slice(2), [
    "-i", "dev", "scan", "--label", "feature-1", "--update-set", id,
    "--target", `sys_script:${id}`, "--suite", "b".repeat(32), "--confirm",
  ]);
  assert.throws(() => sanitizeInstanceArguments("scan", { instance: "dev" }), /confirm=true/);
  assert.throws(() => sanitizeInstanceArguments("scan", { confirm: true }), /explicit instance/);
  for (const bad of [
    { targets: ["sys_script"] }, { updateSets: ["nope"] }, { suite: "1" }, { label: "a b" },
  ]) {
    assert.throws(() => sanitizeInstanceArguments("scan", { instance: "dev", confirm: true, ...bad }),
      TypeError, JSON.stringify(bad));
  }
  const results = sanitizeInstanceArguments("scan-results", {
    instance: "dev", results: [id], progressId: "c".repeat(32), label: "feature-1",
  });
  assert.deepEqual(buildInstanceArguments("scan-results", results).slice(4), [
    "scan-results", "--result", id, "--progress-id", "c".repeat(32), "--label", "feature-1",
  ]);
  assert.throws(() => sanitizeInstanceArguments("scan-results", { suite: id }), TypeError);
});
