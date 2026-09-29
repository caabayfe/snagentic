import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { chmod, mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  buildInstanceInspectionLauncher,
  executeInstanceCommand,
  inspectInstance,
} from "../../.github/extensions/snagentic/instance.mjs";
import {
  EXTENSION_PROTOCOL,
  RUNTIME_ENVIRONMENT_VARIABLES,
  applyRuntimeMarker,
  buildLauncherForArgv,
  findOnPath,
  requestedEnvironmentVariables,
  resolveWorkspaceRoot,
  selectRuntime,
} from "../../.github/extensions/snagentic/lib.mjs";

const NATIVE = "/opt/snagentic/snagentic";

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
        calls.at(-1).stdin = text ?? "";
      },
    };
    child.kill = () => {};
    const response = responses.shift();
    setImmediate(() => {
      child.stdout.emit("data", JSON.stringify(response.stdout ?? {}));
      child.emit("close", response.code ?? 0);
    });
    return child;
  };
}

async function withTempDir(prefix, body) {
  const directory = await mkdtemp(path.join(os.tmpdir(), prefix));
  try {
    return await body(directory);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
}

test("workspace root is the checkout for project extensions and cwd for user extensions", () => {
  const checkout = path.join(path.sep, "repo");
  assert.equal(
    resolveWorkspaceRoot(path.join(checkout, ".github", "extensions", "snagentic"), "/elsewhere"),
    checkout,
  );
  assert.equal(
    resolveWorkspaceRoot(path.join(os.homedir(), ".copilot", "extensions", "snagentic"), "/work"),
    "/work",
  );
});

test("runtime selection prefers explicit overrides, then native, then Docker", () => {
  const none = () => undefined;
  const found = () => "/usr/local/bin/snagentic";
  assert.deepEqual(selectRuntime({ PATH: "/usr/bin" }, { findExecutable: none }), {
    mode: "docker",
    executable: "docker",
  });
  assert.deepEqual(selectRuntime({}, { findExecutable: found }), {
    mode: "native",
    executable: "/usr/local/bin/snagentic",
  });
  assert.deepEqual(selectRuntime({ SNAGENTIC_EXECUTABLE: NATIVE }, { findExecutable: found }), {
    mode: "native",
    executable: NATIVE,
  });
  assert.equal(
    selectRuntime({ SNAGENTIC_PYTHON: "python3" }, { findExecutable: found }).mode,
    "python",
  );
  assert.equal(
    selectRuntime({ SNAGENTIC_RUNTIME: "docker", SNAGENTIC_EXECUTABLE: NATIVE }).mode,
    "docker",
  );
  assert.throws(() => selectRuntime({ SNAGENTIC_RUNTIME: "podman" }), /native, python or docker/);
  assert.throws(() => selectRuntime({ SNAGENTIC_RUNTIME: "python" }), /requires SNAGENTIC_PYTHON/);
  assert.throws(
    () => selectRuntime({ SNAGENTIC_RUNTIME: "native" }, { findExecutable: none }),
    /no snagentic executable/,
  );
  assert.throws(
    () => selectRuntime({ SNAGENTIC_EXECUTABLE: "snagentic --evil" }),
    /absolute path/,
  );
});

test("PATH discovery accepts only executable files in absolute directories", async (t) => {
  if (process.platform === "win32") t.skip("POSIX permission semantics");
  await withTempDir("snag-path-", async (directory) => {
    const binary = path.join(directory, "snagentic");
    await writeFile(binary, "#!/bin/sh\n");
    await chmod(binary, 0o644);
    assert.equal(findOnPath("snagentic", { PATH: `relative:${directory}` }), undefined);
    await chmod(binary, 0o755);
    assert.equal(findOnPath("snagentic", { PATH: `relative:${directory}` }), binary);
    assert.equal(findOnPath("snagentic", { PATH: "relative" }), undefined);
  });
});

test("the install marker supplies the executable without overriding the user", async () => {
  await withTempDir("snag-marker-", async (directory) => {
    assert.equal(applyRuntimeMarker({}, directory), undefined);
    await writeFile(
      path.join(directory, "snagentic-runtime.json"),
      JSON.stringify({ executable: NATIVE, version: "1.2.3", protocol: EXTENSION_PROTOCOL }),
    );
    const source = {};
    assert.deepEqual(applyRuntimeMarker(source, directory), {
      executable: NATIVE,
      version: "1.2.3",
      protocolCompatible: true,
    });
    assert.equal(source.SNAGENTIC_EXECUTABLE, NATIVE);
    const explicit = { SNAGENTIC_EXECUTABLE: "/custom/snagentic" };
    applyRuntimeMarker(explicit, directory);
    assert.equal(explicit.SNAGENTIC_EXECUTABLE, "/custom/snagentic");

    await writeFile(
      path.join(directory, "snagentic-runtime.json"),
      JSON.stringify({ executable: NATIVE, protocol: EXTENSION_PROTOCOL + 1 }),
    );
    assert.equal(applyRuntimeMarker({}, directory).protocolCompatible, false);
    await writeFile(path.join(directory, "snagentic-runtime.json"), "{not json");
    assert.throws(() => applyRuntimeMarker({}, directory), /invalid snagentic-runtime.json/);
  });
});

test("native mode requests no credential variables from Copilot", () => {
  const native = requestedEnvironmentVariables("native", ["SNAGENTIC_ACME_DEV_PASSWORD"], {});
  assert.deepEqual(native, [...RUNTIME_ENVIRONMENT_VARIABLES]);
  assert.ok(!native.some((name) => /TOKEN|PASSWORD|USERNAME/u.test(name)));
  const docker = requestedEnvironmentVariables("docker", ["SNAGENTIC_ACME_DEV_PASSWORD"], {});
  assert.ok(docker.includes("SNAGENTIC_ACME_DEV_PASSWORD"));
  assert.ok(docker.includes("SNAGENTIC_DEV_TOKEN"));
  const optedIn = requestedEnvironmentVariables("native", ["SNAGENTIC_ACME_DEV_PASSWORD"], {
    SNAGENTIC_CREDENTIAL_STORE: "env",
  });
  assert.ok(optedIn.includes("SNAGENTIC_ACME_DEV_PASSWORD"));
});

test("native launchers invoke the executable directly without Docker or Python", () => {
  const source = { PATH: "/usr/bin", HOME: "/home/u", SNAGENTIC_EXECUTABLE: NATIVE };
  const launcher = buildLauncherForArgv(["--json", "instance", "-i", "dev", "status"], source);
  assert.equal(launcher.mode, "native");
  assert.equal(launcher.executable, NATIVE);
  assert.ok(!launcher.argv.includes("compose"));
  assert.ok(!launcher.argv.includes("-m"));
  assert.ok(launcher.argv.includes("status"));

  assert.deepEqual(buildInstanceInspectionLauncher("acme", source).argv, [
    "--json", "instance", "-i", "acme", "profile",
  ]);
});

test("native instance profile results nested under result are parsed", async () => {
  const source = { PATH: "/usr/bin", SNAGENTIC_EXECUTABLE: NATIVE };
  const calls = [];
  const instance = await inspectInstance("acme", {
    sourceEnvironment: source,
    spawnImpl: fakeSpawn(
      [{
        stdout: {
          ok: true,
          result: {
            instance: "acme",
            kind: "development",
            credential_names: ["SNAGENTIC_ACME_PASSWORD", "SNAGENTIC_ACME_USERNAME"],
            store: "keychain",
          },
        },
      }],
      calls,
    ),
  });
  assert.equal(calls[0].executable, NATIVE);
  assert.equal(instance.instance, "acme");
  assert.equal(instance.kind, "development");
});

test("native ui-run executes in one direct call without credential files", async () => {
  await withTempDir("snag-native-home-", async (home) => {
    await mkdir(path.join(home, ".config", "snagentic"), { recursive: true });
    const credentialFile = path.join(home, ".config", "snagentic", "dev.env");
    await writeFile(credentialFile, "SNAGENTIC_DEV_PASSWORD=file-secret\n");
    await chmod(credentialFile, 0o600);
    const calls = [];
    const outcome = await executeInstanceCommand(
      "ui-run",
      { instance: "dev", recipe: "login-check", confirm: true },
      {
        inspect: async () => ({
          instance: "dev",
          kind: "development",
          credentialNames: ["SNAGENTIC_DEV_USERNAME", "SNAGENTIC_DEV_PASSWORD"],
        }),
        sourceEnvironment: { PATH: "/usr/bin", HOME: home, SNAGENTIC_EXECUTABLE: NATIVE },
        spawnImpl: fakeSpawn([{ stdout: { ok: true, result: { logged_in: true } } }], calls),
      },
    );
    assert.equal(outcome.resultType, "success", outcome.textResultForLlm);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].executable, NATIVE);
    assert.ok(!calls[0].argv.includes("--prepare-only"));
    assert.ok(calls[0].argv.includes("ui"));
    assert.ok(!("SNAGENTIC_DEV_PASSWORD" in calls[0].options.env));
    assert.ok(!JSON.stringify(calls[0]).includes("file-secret"));
  });
});

test("native write commands still enforce the development-only guard", async () => {
  const calls = [];
  const outcome = await executeInstanceCommand(
    "apply",
    { instance: "prod", planId: "0123456789abcdef", confirm: true },
    {
      inspect: async () => ({ instance: "prod", kind: "production", credentialNames: [] }),
      sourceEnvironment: { PATH: "/usr/bin", SNAGENTIC_EXECUTABLE: NATIVE },
      spawnImpl: fakeSpawn([], calls),
    },
  );
  assert.equal(outcome.resultType, "denied");
  assert.equal(calls.length, 0);
});
