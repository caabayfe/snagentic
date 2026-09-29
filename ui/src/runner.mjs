// Playwright runner for ServiceNow UI recipes.
//
// The request arrives on stdin from `snagentic instance ui run`. The native CLI includes
// the login as `request.credentials` on that private pipe; the legacy container flow
// sends environment variable *names* instead. Tracing starts after login so the password
// is never recorded; every string leaving the runner is scrubbed.

import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

const RECIPE_NAME = /^[a-z0-9][a-z0-9-]{0,62}$/;
const ENV_NAME = /^[A-Z][A-Z0-9_]{0,127}$/;
const DEFAULT_PATTERN = "^[A-Za-z0-9_.:\\- ]{1,200}$";

export class RecipeError extends Error {}

export function parseRequest(raw) {
  let request;
  try {
    request = JSON.parse(raw);
  } catch {
    throw new RecipeError("request must be JSON");
  }
  const required = ["recipe", "base_url", "repository_root", "evidence_dir"];
  for (const key of required) {
    if (typeof request?.[key] !== "string" || !request[key]) {
      throw new RecipeError(`request.${key} is required`);
    }
  }
  if (!RECIPE_NAME.test(request.recipe)) throw new RecipeError("invalid recipe name");
  const credentials = request.credentials;
  if (credentials !== undefined) {
    if (
      credentials === null ||
      typeof credentials !== "object" ||
      typeof credentials.username !== "string" ||
      typeof credentials.password !== "string" ||
      !credentials.username ||
      !credentials.password
    ) {
      throw new RecipeError("request.credentials must contain username and password");
    }
  } else if (!ENV_NAME.test(request.username_env ?? "") || !ENV_NAME.test(request.password_env ?? "")) {
    throw new RecipeError("credential settings must be environment variable names");
  }
  const url = new URL(request.base_url);
  if (url.protocol !== "https:") throw new RecipeError("base_url must use https");
  const evidence = path.normalize(request.evidence_dir);
  if (path.isAbsolute(evidence) || !evidence.startsWith(".snagentic" + path.sep)) {
    throw new RecipeError("evidence_dir must be inside .snagentic/");
  }
  return {
    ...request,
    base_url: url.origin,
    parameters: request.parameters ?? {},
    headless: request.headless !== false,
    timeout_ms: Number(request.timeout_ms) > 0 ? Number(request.timeout_ms) : 120_000,
  };
}

export async function loadRecipe(recipesRoot, name) {
  if (!RECIPE_NAME.test(name)) throw new RecipeError("invalid recipe name");
  const directory = path.join(recipesRoot, name);
  const manifest = JSON.parse(await readFile(path.join(directory, "recipe.json"), "utf8"));
  if (manifest.name !== name) throw new RecipeError(`recipe manifest name mismatch: ${name}`);
  const module = await import(pathToFileURL(path.join(directory, "steps.mjs")).href);
  if (typeof module.run !== "function") {
    throw new RecipeError(`recipe ${name} must export run()`);
  }
  return { manifest, run: module.run };
}

export function validateParameters(manifest, parameters, repositoryRoot) {
  const spec = manifest.parameters ?? {};
  const unknown = Object.keys(parameters).filter((key) => !(key in spec));
  if (unknown.length) throw new RecipeError(`unsupported parameters: ${unknown.join(", ")}`);
  const result = {};
  for (const [key, rules] of Object.entries(spec)) {
    const value = parameters[key];
    if (value === undefined || value === "") {
      if (rules.required) throw new RecipeError(`missing parameter: ${key}`);
      continue;
    }
    if (typeof value !== "string") throw new RecipeError(`parameter ${key} must be a string`);
    if (rules.type === "file") {
      const root = path.resolve(repositoryRoot);
      const resolved = path.resolve(root, value);
      if (!resolved.startsWith(root + path.sep)) {
        throw new RecipeError(`parameter ${key} must stay inside the repository`);
      }
      result[key] = resolved;
      continue;
    }
    if (!new RegExp(rules.pattern ?? DEFAULT_PATTERN).test(value)) {
      throw new RecipeError(`invalid value for parameter ${key}`);
    }
    result[key] = value;
  }
  return result;
}

export function scrubber(secrets) {
  const values = secrets.filter((value) => typeof value === "string" && value.length >= 3);
  return (text) => {
    let output = String(text);
    for (const value of values) output = output.split(value).join("[REDACTED]");
    return output;
  };
}

export async function login(page, baseUrl, username, password, timeout) {
  await page.goto(`${baseUrl}/login.do`, { waitUntil: "domcontentloaded", timeout });
  await page.locator("#user_name").fill(username);
  await page.locator("#user_password").fill(password);
  await Promise.all([
    page.waitForLoadState("domcontentloaded", { timeout }),
    page.locator("#sysverb_login").click(),
  ]);
  await page.waitForTimeout(500);
  const stillOnLogin =
    new URL(page.url()).pathname.endsWith("/login.do") &&
    (await page.locator("#user_password").count()) > 0;
  if (stillOnLogin) {
    throw new RecipeError("login failed (check the local UI user and that SSO is not enforced)");
  }
}

// Classic forms render inside gsft_main when opened through the Next Experience shell.
export function classicFrame(page) {
  const frame = page.frame({ name: "gsft_main" });
  return frame ?? page.mainFrame();
}

export async function execute(request, { chromium, environ = process.env, recipesRoot }) {
  const { credentials } = request;
  delete request.credentials;
  const username = credentials?.username ?? environ[request.username_env];
  const password = credentials?.password ?? environ[request.password_env];
  if (!username || !password) {
    throw new RecipeError(
      `missing credential environment variables: ${request.username_env}, ${request.password_env}`,
    );
  }
  const scrub = scrubber([password]);
  const { manifest, run } = await loadRecipe(recipesRoot, request.recipe);
  const parameters = validateParameters(manifest, request.parameters, request.repository_root);
  const evidenceDir = path.resolve(request.repository_root, request.evidence_dir);
  await mkdir(evidenceDir, { recursive: true });

  const steps = [];
  const browser = await chromium.launch({ headless: request.headless });
  let context;
  let page;
  let traceStarted = false;
  try {
    context = await browser.newContext({ ignoreHTTPSErrors: false });
    page = await context.newPage();
    page.setDefaultTimeout(request.timeout_ms);
    await login(page, request.base_url, username, password, request.timeout_ms);
    await context.tracing.start({ screenshots: true, snapshots: true });
    traceStarted = true;
    let counter = 0;
    const step = async (name, action) => {
      counter += 1;
      const started = Date.now();
      try {
        const value = await action();
        steps.push({ step: name, ok: true, ms: Date.now() - started });
        return value;
      } finally {
        const file = `${String(counter).padStart(2, "0")}-${name.replace(/[^a-z0-9-]+/gi, "-")}.png`;
        await page.screenshot({ path: path.join(evidenceDir, file), fullPage: true }).catch(() => {});
      }
    };
    const result = await run({
      page,
      baseUrl: request.base_url,
      parameters,
      step,
      timeout: request.timeout_ms,
      classicFrame: () => classicFrame(page),
      log: (message) => steps.push({ note: scrub(message) }),
    });
    return { ok: true, result: JSON.parse(scrub(JSON.stringify(result ?? {}))), steps };
  } catch (error) {
    if (page) {
      await page.screenshot({ path: path.join(evidenceDir, "failure.png"), fullPage: true })
        .catch(() => {});
    }
    steps.push({ step: "failure", ok: false });
    return { ok: false, error: scrub(error?.message ?? String(error)).slice(0, 1000), steps };
  } finally {
    if (traceStarted) {
      await context.tracing.stop({ path: path.join(evidenceDir, "trace.zip") }).catch(() => {});
    }
    await browser.close().catch(() => {});
  }
}

export async function writeResult(request, outcome) {
  const evidenceDir = path.resolve(request.repository_root, request.evidence_dir);
  await mkdir(evidenceDir, { recursive: true });
  await writeFile(path.join(evidenceDir, "result.json"), JSON.stringify(outcome, null, 2) + "\n");
}
