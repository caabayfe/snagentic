// Standards-compliant Model Context Protocol (MCP) server for the snagentic instance
// mirror workflow. It exposes the same tool catalogue, JSON Schemas and safety gates as
// the GitHub Copilot CLI extension (extension.mjs) -- both import `instanceTools` from
// instance.mjs -- so any MCP-compatible client (not only Copilot CLI) gets identical,
// centrally enforced behaviour: development-only writes, confirm gates, OS-keychain
// credentials, and argv-only process launches with no shell interpolation.
//
// Run directly with `node mcp-server.mjs` (stdio transport), or via `snagentic mcp serve`,
// which resolves the bundled Node runtime and this script for the installed CLI.

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";

import { applyRuntimeFile, applyRuntimeMarker } from "./lib.mjs";
import { instanceTools } from "./instance.mjs";

export const SERVER_NAME = "snagentic";

/** Builds the MCP `tools/list` payload from the shared instance tool catalogue. */
export function listToolDefinitions() {
  return instanceTools.map((tool) => ({
    name: tool.name,
    description: tool.description,
    inputSchema: tool.parameters,
    // skipPermission mirrors Copilot CLI's own permission prompt: tools that change a
    // ServiceNow instance (skipPermission: false) are annotated as destructive so any
    // MCP client can surface its own confirmation UX before calling them. The actual
    // gate (development-only, confirm=true) is enforced by executeInstanceCommand
    // itself, regardless of what a client does with these hints.
    annotations: tool.skipPermission === false
      ? { destructiveHint: true, idempotentHint: false, openWorldHint: true }
      : { readOnlyHint: true, destructiveHint: false, openWorldHint: true },
  }));
}

const toolsByName = new Map(instanceTools.map((tool) => [tool.name, tool]));

/** Looks up and runs one tool by MCP name, returning an MCP CallToolResult. */
export async function callTool(name, rawArguments) {
  const tool = toolsByName.get(name);
  if (!tool) {
    return {
      isError: true,
      content: [{ type: "text", text: JSON.stringify({ ok: false, error: `unknown tool: ${name}` }) }],
    };
  }
  let outcome;
  try {
    outcome = await tool.handler(rawArguments ?? {});
  } catch (error) {
    outcome = {
      resultType: "failure",
      textResultForLlm: JSON.stringify({
        ok: false,
        error: error instanceof Error ? error.message : String(error),
      }),
    };
  }
  return {
    isError: outcome.resultType !== "success",
    content: [{ type: "text", text: outcome.textResultForLlm }],
  };
}

export function createServer({ version = "0.0.0" } = {}) {
  const server = new Server(
    { name: SERVER_NAME, version },
    { capabilities: { tools: {} } },
  );
  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: listToolDefinitions() }));
  server.setRequestHandler(CallToolRequestSchema, async (request) =>
    callTool(request.params.name, request.params.arguments));
  return server;
}

async function main() {
  // stdout is reserved for JSON-RPC frames; startup diagnostics must go to stderr only.
  applyRuntimeFile(process.env);
  let version = "0.0.0";
  try {
    const marker = applyRuntimeMarker(process.env);
    if (marker?.version) version = marker.version;
    if (marker && !marker.protocolCompatible) {
      console.error(
        `snagentic: installed ${marker.version ?? "unknown"} does not match the extension ` +
          "protocol; run `snagentic copilot install` again",
      );
    }
  } catch (error) {
    console.error(`snagentic: ${error instanceof Error ? error.message : String(error)}`);
  }
  const server = createServer({ version });
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

const isMain = (() => {
  try {
    return import.meta.url === new URL(`file://${process.argv[1]}`).href;
  } catch {
    return false;
  }
})();

if (isMain) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.stack ?? error.message : String(error));
    process.exitCode = 1;
  });
}
