import assert from "node:assert/strict";
import { mkdtemp, readdir, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  execute,
  loadRecipe,
  parseRequest,
  RecipeError,
  scrubber,
  validateParameters,
} from "../../ui/src/runner.mjs";

const RECIPES = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../ui/recipes");
const BASE = {
  recipe: "login-check",
  base_url: "https://dev.example.service-now.com/",
  username_env: "SNAGENTIC_DEV_USERNAME",
  password_env: "SNAGENTIC_DEV_PASSWORD",
  repository_root: "/repo",
  evidence_dir: ".snagentic/dev/ui/run-1",
};

test("parseRequest validates and normalizes the request", () => {
  const request = parseRequest(JSON.stringify(BASE));
  assert.equal(request.base_url, "https://dev.example.service-now.com");
  assert.equal(request.headless, true);
  assert.throws(() => parseRequest(JSON.stringify({ ...BASE, base_url: "http://x" })), RecipeError);
  assert.throws(
    () => parseRequest(JSON.stringify({ ...BASE, evidence_dir: "../elsewhere" })),
    RecipeError,
  );
  assert.throws(
    () => parseRequest(JSON.stringify({ ...BASE, password_env: "secret value" })),
    RecipeError,
  );
});

test("every bundled recipe loads and declares its parameters", async () => {
  for (const name of await readdir(RECIPES)) {
    const { manifest, run } = await loadRecipe(RECIPES, name);
    assert.equal(manifest.name, name);
    assert.equal(typeof run, "function");
    assert.equal(typeof manifest.mutating, "boolean");
    assert.ok(Array.isArray(manifest.steps) && manifest.steps.length > 0);
  }
});

test("validateParameters enforces patterns and repository-bound files", async () => {
  const { manifest } = await loadRecipe(RECIPES, "activate-plugin");
  assert.deepEqual(validateParameters(manifest, { plugin_id: "com.snc.x" }, "/repo"), {
    plugin_id: "com.snc.x",
  });
  assert.throws(() => validateParameters(manifest, {}, "/repo"), /missing/);
  assert.throws(() => validateParameters(manifest, { plugin_id: "a;b" }, "/repo"), /invalid/);
  assert.throws(
    () => validateParameters(manifest, { plugin_id: "x", y: "1" }, "/repo"),
    /unsupported/,
  );
  const upload = (await loadRecipe(RECIPES, "upload-update-set")).manifest;
  assert.throws(() => validateParameters(upload, { file: "../../etc/passwd" }, "/repo"), /inside/);

  const incidentTask = (await loadRecipe(RECIPES, "incident-task-create")).manifest;
  assert.deepEqual(
    validateParameters(incidentTask, {
      incident_sys_id: "0123456789abcdef0123456789abcdef",
      scenario: "cancel",
      short_description: "Validate incident task creation",
    }, "/repo"),
    {
      incident_sys_id: "0123456789abcdef0123456789abcdef",
      scenario: "cancel",
      short_description: "Validate incident task creation",
    },
  );
  assert.throws(
    () => validateParameters(incidentTask, { incident_sys_id: "not-a-sys-id" }, "/repo"),
    /invalid/,
  );
  assert.throws(
    () => validateParameters(incidentTask, { scenario: "delete" }, "/repo"),
    /invalid/,
  );
});

test("scrubber removes secrets from output", () => {
  assert.equal(scrubber(["hunter22"])("bad hunter22 value"), "bad [REDACTED] value");
});

function fakeChromium(log, { loginFails = false, statsText = "" } = {}) {
  let url = "about:blank";
  const page = {
    setDefaultTimeout() {},
    async goto(target) {
      url = target;
      log.push(["goto", new URL(target).pathname]);
    },
    url: () => url,
    locator(selector) {
      return {
        async fill(value) {
          log.push(["fill", selector, selector === "#user_password" ? "***" : value]);
        },
        async click() {
          if (selector === "#sysverb_login" && !loginFails) url = "https://dev/now/nav/ui/home";
        },
        async count() {
          return url.endsWith("/login.do") ? 1 : 0;
        },
        async innerText() {
          return statsText;
        },
      };
    },
    async waitForLoadState() {},
    async waitForTimeout() {},
    async screenshot({ path: file }) {
      log.push(["screenshot", path.basename(file)]);
    },
  };
  const context = {
    newPage: async () => page,
    tracing: {
      async start() {
        log.push(["trace-start"]);
      },
      async stop() {
        log.push(["trace-stop"]);
      },
    },
  };
  return {
    async launch() {
      return { newContext: async () => context, close: async () => log.push(["close"]) };
    },
  };
}

const CREDENTIALS = { SNAGENTIC_DEV_USERNAME: "agent", SNAGENTIC_DEV_PASSWORD: "p4ssw0rd!" };

test("execute logs in before tracing and returns the recipe result", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "snagentic-ui-"));
  try {
    const log = [];
    const request = parseRequest(JSON.stringify({ ...BASE, repository_root: root }));
    const outcome = await execute(request, {
      chromium: fakeChromium(log, { statsText: "Build name: Australia\nBuild tag: glide-x\n" }),
      environ: CREDENTIALS,
      recipesRoot: RECIPES,
    });
    assert.equal(outcome.ok, true, outcome.error);
    assert.equal(outcome.result.build_name, "Australia");
    const fillIndex = log.findIndex(
      (entry) => entry[0] === "fill" && entry[1] === "#user_password",
    );
    const traceIndex = log.findIndex((entry) => entry[0] === "trace-start");
    assert.ok(fillIndex >= 0 && traceIndex > fillIndex, "tracing must start after login");
    assert.ok(log.some((entry) => entry[0] === "screenshot" && entry[1] === "01-stats.png"));
    assert.deepEqual(log.at(-1), ["close"]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("execute reports login failure without leaking the password", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "snagentic-ui-"));
  try {
    const request = parseRequest(JSON.stringify({ ...BASE, repository_root: root }));
    const outcome = await execute(request, {
      chromium: fakeChromium([], { loginFails: true }),
      environ: CREDENTIALS,
      recipesRoot: RECIPES,
    });
    assert.equal(outcome.ok, false);
    assert.match(outcome.error, /login failed/);
    assert.ok(!JSON.stringify(outcome).includes("p4ssw0rd!"));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("stdin credentials replace environment names and never reach the result", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "snagentic-ui-"));
  try {
    const { username_env: _u, password_env: _p, ...withoutNames } = BASE;
    assert.throws(
      () => parseRequest(JSON.stringify({ ...withoutNames, repository_root: root })),
      RecipeError,
    );
    assert.throws(
      () => parseRequest(JSON.stringify({
        ...withoutNames,
        repository_root: root,
        credentials: { username: "agent" },
      })),
      /credentials/,
    );
    const request = parseRequest(JSON.stringify({
      ...withoutNames,
      repository_root: root,
      credentials: { username: "agent", password: "k3ych41n!" },
    }));
    const log = [];
    const outcome = await execute(request, {
      chromium: fakeChromium(log, { statsText: "Build name: Australia\n" }),
      environ: {},
      recipesRoot: RECIPES,
    });
    assert.equal(outcome.ok, true, outcome.error);
    assert.ok(log.some((entry) => entry[0] === "fill" && entry[2] === "agent"));
    assert.equal(request.credentials, undefined);
    assert.ok(!JSON.stringify(outcome).includes("k3ych41n!"));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
