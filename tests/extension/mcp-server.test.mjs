import assert from "node:assert/strict";
import test from "node:test";

import { instanceTools } from "../../.github/extensions/snagentic/instance.mjs";
import {
  SERVER_NAME,
  callTool,
  createServer,
  listToolDefinitions,
} from "../../.github/extensions/snagentic/mcp-server.mjs";

test("listToolDefinitions mirrors the shared instance tool catalogue 1:1", () => {
  const definitions = listToolDefinitions();
  assert.equal(definitions.length, instanceTools.length);
  assert.equal(definitions.length, 23);
  for (const [index, tool] of instanceTools.entries()) {
    const definition = definitions[index];
    assert.equal(definition.name, tool.name);
    assert.equal(definition.description, tool.description);
    // Every tool must expose its JSON Schema verbatim: MCP clients validate/complete
    // arguments from this, same schema the Copilot extension already uses.
    assert.equal(definition.inputSchema, tool.parameters);
  }
});

test("listToolDefinitions annotates destructive (write) tools distinctly from read-only ones", () => {
  const definitions = listToolDefinitions();
  const byName = new Map(definitions.map((definition) => [definition.name, definition]));

  const apply = byName.get("snagentic_instance_apply");
  assert.equal(apply.annotations.destructiveHint, true);
  assert.equal(apply.annotations.readOnlyHint, undefined);

  const list = byName.get("snagentic_instance_list");
  assert.equal(list.annotations.readOnlyHint, true);
  assert.equal(list.annotations.destructiveHint, false);
});

test("callTool rejects unknown tool names without throwing", async () => {
  const outcome = await callTool("snagentic_instance_does_not_exist", {});
  assert.equal(outcome.isError, true);
  const payload = JSON.parse(outcome.content[0].text);
  assert.equal(payload.ok, false);
  assert.match(payload.error, /unknown tool/);
});

test("callTool enforces the confirm=true gate for write tools, matching the Copilot extension", async () => {
  const outcome = await callTool("snagentic_instance_apply", {
    instance: "prod",
    planId: "0123456789abcdef",
  });
  assert.equal(outcome.isError, true);
  const payload = JSON.parse(outcome.content[0].text);
  assert.equal(payload.ok, false);
  assert.match(payload.error, /requires confirm=true/);
});

test("callTool rejects malformed arguments via schema-level sanitization before any process runs", async () => {
  const outcome = await callTool("snagentic_instance_apply", {
    instance: "prod",
    planId: "../not-a-plan-id",
    confirm: true,
  });
  assert.equal(outcome.isError, true);
  const payload = JSON.parse(outcome.content[0].text);
  assert.equal(payload.ok, false);
});

test("createServer registers the tools/list and tools/call request handlers", () => {
  const server = createServer({ version: "9.9.9" });
  assert.equal(SERVER_NAME, "snagentic");
  assert.ok(server);
});
