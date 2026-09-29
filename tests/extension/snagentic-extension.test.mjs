import assert from "node:assert/strict";
import test from "node:test";

import {
  MAX_OUTPUT_BYTES,
  REQUESTED_ENVIRONMENT_VARIABLES,
  buildCliLauncher,
  buildDockerArguments,
  buildInspectionLauncher,
  buildPythonArguments,
  childEnvironment,
  containerEnvironmentNames,
  formatCappedOutput,
  joinSessionWithPermissionFallback,
  parseCliFailure,
  pushPermissionDecision,
  sanitizeArguments,
  selectPythonOverride,
  truncateUtf8,
  validateCredentialNames,
} from "../../.github/extensions/snagentic/lib.mjs";

test("credential permission denial falls back to registering tools without environment access", async () => {
  const calls = [];
  const session = { log: async () => {} };
  const result = await joinSessionWithPermissionFallback(async (options) => {
    calls.push(options);
    if (calls.length === 1) {
      throw new Error(
        'Extension "project:snagentic" was denied permission access and will not be loaded.',
      );
    }
    return session;
  }, {
    requestedEnvironmentVariables: ["SNAGENTIC_DEV_TOKEN"],
    tools: [{ name: "snagentic_status" }],
  });

  assert.equal(result.session, session);
  assert.equal(result.environmentAccessDenied, true);
  assert.deepEqual(calls, [
    {
      requestedEnvironmentVariables: ["SNAGENTIC_DEV_TOKEN"],
      tools: [{ name: "snagentic_status" }],
    },
    {
      tools: [{ name: "snagentic_status" }],
    },
  ]);
});

test("session registration errors other than credential denial are not hidden", async () => {
  const failure = new Error("tool name collision");
  await assert.rejects(
    joinSessionWithPermissionFallback(async () => {
      throw failure;
    }, {
      requestedEnvironmentVariables: ["SNAGENTIC_DEV_TOKEN"],
      tools: [],
    }),
    (error) => error === failure,
  );
});

test("builds argv without shell interpolation", () => {
  const args = sanitizeArguments("query", {
    config: "config/snagentic.yaml",
    environment: "dev",
    text: "incident workflow",
    domain: "global",
    artifactType: "script-include",
  });

  assert.deepEqual(buildPythonArguments("query", args), [
    "-m",
    "snagentic",
    "--json",
    "--config",
    "config/snagentic.yaml",
    "--environment",
    "dev",
    "query",
    "incident workflow",
    "--domain",
    "global",
    "--artifact-type",
    "script-include",
  ]);
});

test("rejects unknown arguments and unsafe config paths", () => {
  assert.throws(
    () => sanitizeArguments("status", { unexpected: true }),
    /unsupported argument/u,
  );
  assert.throws(
    () => sanitizeArguments("status", { config: "../outside.yaml" }),
    /parent traversal/u,
  );
});

test("push requires explicit confirmation and always adds approve", () => {
  assert.throws(
    () => sanitizeArguments("push", { confirm: true }),
    /explicit environment/u,
  );
  assert.throws(
    () => sanitizeArguments("push", { environment: "dev" }),
    /confirm=true/u,
  );
  const args = sanitizeArguments("push", { environment: "dev", confirm: true });
  assert.deepEqual(buildPythonArguments("push", args), [
    "-m",
    "snagentic",
    "--json",
    "--environment",
    "dev",
    "push",
    "--approve",
  ]);
});

test("permission hook uses configured profile kind rather than environment name", async () => {
  assert.equal(
    (
      await pushPermissionDecision(
        {
          toolName: "snagentic_push",
          toolArgs: { environment: "looks-like-dev", confirm: true },
        },
        {
          inspectProfile: async () => ({
            environment: "looks-like-dev",
            kind: "production",
            credentialNames: ["SNAGENTIC_PROD_TOKEN"],
          }),
        },
      )
    ).permissionDecision,
    "deny",
  );
  assert.equal(
    (
      await pushPermissionDecision(
        {
          toolName: "snagentic_push",
          toolArgs: { environment: "release-sandbox", confirm: true },
        },
        {
          inspectProfile: async () => ({
            environment: "release-sandbox",
            kind: "development",
            credentialNames: ["SNAGENTIC_DEV_TOKEN"],
          }),
        },
      )
    ).permissionDecision,
    "ask",
  );
  assert.equal(
    (
      await pushPermissionDecision({
        toolName: "snagentic_push",
        toolArgs: { confirm: true },
      })
    ).permissionDecision,
    "deny",
  );
  assert.equal(
    (
      await pushPermissionDecision(
        {
          toolName: "snagentic_push",
          toolArgs: { environment: "dev", confirm: true },
        },
        {
          inspectProfile: async () => {
            throw new Error("invalid YAML");
          },
        },
      )
    ).permissionDecision,
    "deny",
  );
});

test("uses Docker by default and preserves an explicit safe Python override", () => {
  assert.equal(selectPythonOverride({}), undefined);
  assert.equal(
    selectPythonOverride({ SNAGENTIC_PYTHON: "/opt/snagentic/bin/python" }),
    "/opt/snagentic/bin/python",
  );
  assert.throws(
    () => selectPythonOverride({ SNAGENTIC_PYTHON: "python3 -I" }),
    /simple executable name/u,
  );

  const dockerLauncher = buildCliLauncher("status", {}, { PATH: "/usr/bin" });
  assert.equal(dockerLauncher.executable, "docker");
  assert.deepEqual(dockerLauncher.argv, [
    "compose",
    "run",
    "--rm",
    "-T",
    "cli",
    "--json",
    "status",
  ]);

  const pythonLauncher = buildCliLauncher(
    "status",
    {},
    { PATH: "/usr/bin", SNAGENTIC_PYTHON: "/opt/snagentic/bin/python" },
  );
  assert.equal(pythonLauncher.executable, "/opt/snagentic/bin/python");
  assert.deepEqual(pythonLauncher.argv, ["-m", "snagentic", "--json", "status"]);
});

test("child environment preserves enterprise transport variables and selected credentials", () => {
  const source = {
    PATH: "/usr/bin",
    HOME: "/tmp/home",
    HTTPS_PROXY: "https://proxy.example",
    http_proxy: "http://proxy.example",
    NO_PROXY: "localhost",
    SSL_CERT_FILE: "/etc/company-ca.pem",
    REQUESTS_CA_BUNDLE: "/etc/requests-only.pem",
    SNAGENTIC_PYTHON: "/opt/snagentic/bin/python",
    SNAGENTIC_DEV_TOKEN: "secret",
    SNAGENTIC_PROD_TOKEN: "must-not-pass",
    GITHUB_TOKEN: "must-not-pass",
    UNRELATED_SECRET: "must-not-pass",
  };
  assert.deepEqual(childEnvironment(source, ["SNAGENTIC_DEV_TOKEN"]), {
    HOME: "/tmp/home",
    HTTPS_PROXY: "https://proxy.example",
    NO_PROXY: "localhost",
    PATH: "/usr/bin",
    SSL_CERT_FILE: "/etc/company-ca.pem",
    http_proxy: "http://proxy.example",
    SNAGENTIC_DEV_TOKEN: "secret",
    PYTHONUNBUFFERED: "1",
  });
  assert.equal(REQUESTED_ENVIRONMENT_VARIABLES.includes("SNAGENTIC_PYTHON"), true);
  assert.equal(REQUESTED_ENVIRONMENT_VARIABLES.includes("SNAGENTIC_DEV_TOKEN"), true);
});

test("Docker argv forwards only approved environment names and never values", () => {
  const source = {
    PATH: "/usr/bin",
    HTTPS_PROXY: "https://proxy-user:proxy-secret@proxy.example",
    SSL_CERT_FILE: "/etc/company-ca.pem",
    SNAGENTIC_DEV_TOKEN: "service-now-secret",
    GITHUB_TOKEN: "github-secret",
  };
  assert.deepEqual(containerEnvironmentNames(source, ["SNAGENTIC_DEV_TOKEN"]), [
    "SNAGENTIC_DEV_TOKEN",
    "HTTPS_PROXY",
    "SSL_CERT_FILE",
  ]);
  const argv = buildDockerArguments(
    "inventory",
    { environment: "dev" },
    source,
    ["SNAGENTIC_DEV_TOKEN"],
  );
  assert.deepEqual(argv, [
    "compose",
    "run",
    "--rm",
    "-T",
    "-e",
    "SNAGENTIC_DEV_TOKEN",
    "-e",
    "HTTPS_PROXY",
    "-e",
    "SSL_CERT_FILE",
    "cli",
    "--json",
    "--environment",
    "dev",
    "inventory",
  ]);
  assert.equal(argv.includes("service-now-secret"), false);
  assert.equal(argv.includes("proxy-secret"), false);
  assert.equal(argv.includes("GITHUB_TOKEN"), false);
});

test("Docker argv forwards selected basic-auth names without credential values", () => {
  const source = {
    PATH: "/usr/bin",
    SNAGENTIC_DEV_USERNAME: "basic-user",
    SNAGENTIC_DEV_PASSWORD: "basic-password",
    SNAGENTIC_PROD_PASSWORD: "must-not-pass",
  };
  const argv = buildDockerArguments(
    "inventory",
    { environment: "dev" },
    source,
    ["SNAGENTIC_DEV_USERNAME", "SNAGENTIC_DEV_PASSWORD"],
  );
  assert.equal(argv.includes("SNAGENTIC_DEV_USERNAME"), true);
  assert.equal(argv.includes("SNAGENTIC_DEV_PASSWORD"), true);
  assert.equal(argv.includes("SNAGENTIC_PROD_PASSWORD"), false);
  assert.equal(argv.includes("basic-user"), false);
  assert.equal(argv.includes("basic-password"), false);
});

test("configuration inspection uses Python inside the cli container by default", () => {
  const launcher = buildInspectionLauncher(
    {
      hostPath: "/repo/config/snagentic.yaml",
      containerPath: "/workspace/config/snagentic.yaml",
    },
    "dev",
    { PATH: "/usr/bin", HTTPS_PROXY: "https://proxy.example" },
  );
  assert.equal(launcher.executable, "docker");
  assert.deepEqual(launcher.argv.slice(0, 10), [
    "compose",
    "run",
    "--rm",
    "-T",
    "-e",
    "HTTPS_PROXY",
    "--entrypoint",
    "python",
    "cli",
    "-c",
  ]);
  assert.equal(launcher.argv.at(-2), "/workspace/config/snagentic.yaml");
  assert.equal(launcher.argv.at(-1), "dev");
  assert.equal(launcher.argv.includes("/repo/config/snagentic.yaml"), false);
});

test("rejects credential variables outside the reviewed static allowlist", () => {
  assert.deepEqual(validateCredentialNames(["SNAGENTIC_DEV_TOKEN"]), [
    "SNAGENTIC_DEV_TOKEN",
  ]);
  assert.throws(
    () => validateCredentialNames(["TEAM_PRIVATE_SERVICENOW_TOKEN"]),
    /unapproved credential variable/u,
  );
});

test("parses JSON CLI errors into clean structured failures", () => {
  const result = parseCliFailure(
    '{"ok":false,"error":"required environment variable is unset: SNAGENTIC_DEV_TOKEN"}',
    "fallback",
  );
  assert.equal(result.resultType, "failure");
  assert.deepEqual(JSON.parse(result.textResultForLlm), {
    ok: false,
    error: "required environment variable is unset: SNAGENTIC_DEV_TOKEN",
  });
});

test("caps final output on valid UTF-8 boundaries", () => {
  assert.equal(truncateUtf8("abc😀def", 6), "abc");
  const result = formatCappedOutput(
    { ok: true },
    `{"value":"${"😀".repeat(MAX_OUTPUT_BYTES)}"}`,
    42,
  );
  assert.ok(Buffer.byteLength(result) <= MAX_OUTPUT_BYTES);
  const parsed = JSON.parse(result);
  assert.equal(parsed.ok, true);
  assert.equal(parsed.truncated, true);
  assert.equal(parsed.outputPrefix.includes("\uFFFD"), false);
});
