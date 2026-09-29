#!/usr/bin/env node
// Entry point used by `snagentic instance ui run`: reads one JSON request on stdin
// and prints one JSON result line on stdout.

import path from "node:path";
import { fileURLToPath } from "node:url";

import { execute, parseRequest, writeResult } from "./runner.mjs";

const RECIPES_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../recipes");

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

let request;
try {
  request = parseRequest(await readStdin());
  const { chromium } = await import("playwright");
  const outcome = await execute(request, { chromium, recipesRoot: RECIPES_ROOT });
  await writeResult(request, outcome);
  process.stdout.write(JSON.stringify(outcome) + "\n");
  process.exitCode = outcome.ok ? 0 : 1;
} catch (error) {
  process.stdout.write(JSON.stringify({ ok: false, error: String(error?.message ?? error) }) + "\n");
  process.exitCode = 2;
}
