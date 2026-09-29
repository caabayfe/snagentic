import assert from "node:assert/strict";
import test from "node:test";

import {
  MAX_OUTPUT_BYTES,
  REQUESTED_ENVIRONMENT_VARIABLES,
  buildLauncherForArgv,
  childEnvironment,
  containerEnvironmentNames,
  formatCappedOutput,
  joinSessionWithPermissionFallback,
  parseCliFailure,
  selectPythonOverride,
  truncateUtf8,
  validateCredentialNames,
} from "../../.github/extensions/snagentic/lib.mjs";
import { buildInstanceInspectionLauncher } from "../../.github/extensions/snagentic/instance.mjs";

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
    tools: [{ name: "snagentic_instance_status" }],
  });

  assert.equal(result.session, session);
  assert.equal(result.environmentAccessDenied, true);
  assert.deepEqual(calls, [
    {
      requestedEnvironmentVariables: ["SNAGENTIC_DEV_TOKEN"],
      tools: [{ name: "snagentic_instance_status" }],
    },
    {
      tools: [{ name: "snagentic_instance_status" }],
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

  const dockerLauncher = buildLauncherForArgv(
    ["--json", "instance", "-i", "dev", "status"],
    { PATH: "/usr/bin" },
  );
  assert.equal(dockerLauncher.executable, "docker");
  assert.deepEqual(dockerLauncher.argv, [
    "compose",
    "run",
    "--rm",
    "-T",
    "cli",
    "--json",
    "instance",
    "-i",
    "dev",
    "status",
  ]);

  const pythonLauncher = buildLauncherForArgv(
    ["--json", "instance", "-i", "dev", "status"],
    { PATH: "/usr/bin", SNAGENTIC_PYTHON: "/opt/snagentic/bin/python" },
  );
  assert.equal(pythonLauncher.executable, "/opt/snagentic/bin/python");
  assert.deepEqual(pythonLauncher.argv, [
    "-m", "snagentic", "--json", "instance", "-i", "dev", "status",
  ]);
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
  const argv = buildLauncherForArgv(
    ["--json", "instance", "-i", "dev", "list"],
    source,
    ["SNAGENTIC_DEV_TOKEN"],
  ).argv;
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
    "instance",
    "-i",
    "dev",
    "list",
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
  const argv = buildLauncherForArgv(
    ["--json", "instance", "-i", "dev", "list"],
    source,
    ["SNAGENTIC_DEV_USERNAME", "SNAGENTIC_DEV_PASSWORD"],
  ).argv;
  assert.equal(argv.includes("SNAGENTIC_DEV_USERNAME"), true);
  assert.equal(argv.includes("SNAGENTIC_DEV_PASSWORD"), true);
  assert.equal(argv.includes("SNAGENTIC_PROD_PASSWORD"), false);
  assert.equal(argv.includes("basic-user"), false);
  assert.equal(argv.includes("basic-password"), false);
});

test("instance inspection uses the cli container by default", () => {
  const launcher = buildInstanceInspectionLauncher(
    "dev",
    { PATH: "/usr/bin", HTTPS_PROXY: "https://proxy.example" },
  );
  assert.equal(launcher.executable, "docker");
  assert.deepEqual(launcher.argv.slice(0, 7), [
    "compose",
    "run",
    "--rm",
    "-T",
    "--entrypoint",
    "python",
    "cli",
  ]);
  assert.equal(launcher.argv.at(-1), "dev");
  assert.equal(launcher.argv.includes("config/snagentic.yaml"), false);
  assert.match(launcher.argv.at(-2), /InstanceRegistry/u);
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
