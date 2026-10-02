import { existsSync } from "node:fs";

import { joinSession } from "@github/copilot-sdk/extension";

import {
  EXTENSION_PROTOCOL,
  applyRuntimeFile,
  applyRuntimeMarker,
  joinSessionWithPermissionFallback,
  requestedEnvironmentVariables,
  selectRuntime,
  usesEnvironmentCredentials,
} from "./lib.mjs";
import { discoverInstanceCredentialNames, instanceTools } from "./instance.mjs";

applyRuntimeFile(process.env);
const startupWarnings = [];
let marker;
try {
  marker = applyRuntimeMarker(process.env);
} catch (error) {
  startupWarnings.push(error instanceof Error ? error.message : String(error));
}
if (marker && !marker.protocolCompatible) {
  startupWarnings.push(
    `the installed snagentic ${marker.version ?? ""} does not match extension protocol ` +
      `${EXTENSION_PROTOCOL}; run \`snagentic copilot install\` again`,
  );
}
if (marker && !existsSync(marker.executable)) {
  startupWarnings.push(
    `the snagentic executable ${marker.executable} is missing; reinstall snagentic and run ` +
      "`snagentic copilot install`",
  );
}
let runtimeMode = "docker";
try {
  runtimeMode = selectRuntime(process.env).mode;
} catch (error) {
  startupWarnings.push(error instanceof Error ? error.message : String(error));
}

// The shared tool catalogue (names, descriptions, JSON Schemas and handlers) lives in
// instance.mjs so the Copilot CLI extension and the MCP server (mcp-server.mjs) expose
// identical capabilities and safety gates.
const extensionOptions = {
  requestedEnvironmentVariables: requestedEnvironmentVariables(
    runtimeMode,
    usesEnvironmentCredentials(runtimeMode) ? discoverInstanceCredentialNames() : [],
  ),
  tools: instanceTools,
};

const { session, environmentAccessDenied } = await joinSessionWithPermissionFallback(
  joinSession,
  extensionOptions,
);
if (environmentAccessDenied) {
  await session.log(
    "snagentic loaded without access to requested environment variables; " +
      "local tools remain available. Store ServiceNow credentials with " +
      "`snagentic auth login` so remote commands can use the OS credential store.",
    { level: "warning" },
  );
}
for (const warning of startupWarnings) {
  await session.log(`snagentic: ${warning}`, { level: "warning" });
}
