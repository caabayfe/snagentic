const INSTALL = /^\s*(Install|Update|Upgrade)\s*$/i;
const CONFIRM = /^\s*(Install|Update|Upgrade|Continue|Accept)\s*$/i;
const COMPLETE =
  /(Application installation|Installation|Application install).*(complete|completed|successful|succeeded)|Close & Reload/i;

export async function run({ page, baseUrl, parameters, step, timeout }) {
  const scope = parameters.scope;
  const name = parameters.name ?? scope;
  await step("open-application-manager", () =>
    page.goto(`${baseUrl}/now/app-manager/home`, { waitUntil: "domcontentloaded" }),
  );
  await step("find-application", async () => {
    const search = page.locator("sn-app-mgr-search-input input")
      .or(page.locator('input[type="search"]'))
      .or(page.getByRole("searchbox"))
      .first();
    await search.waitFor({ state: "visible", timeout });
    await search.fill(name);
    const result = page.getByText(name, { exact: true }).first();
    await result.waitFor({ state: "visible", timeout });
    await result.click();
  });

  const action = page.getByRole("link", { name: INSTALL })
    .or(page.getByRole("button", { name: INSTALL })).first();
  await action.waitFor({ state: "visible", timeout })
    .catch(() => {
      throw new Error(`install action unavailable for application: ${scope}`);
    });

  await step("start-installation", () => action.click());
  await step("confirm-dialog", async () => {
    const developerBlock = page.getByText(/unavailable on developer instances/i).first();
    const confirm = page.getByRole("button", { name: CONFIRM }).last();
    const outcome = await Promise.race([
      developerBlock.waitFor({ state: "visible", timeout: 30_000 }).then(() => "blocked"),
      confirm.waitFor({ state: "visible", timeout: 30_000 }).then(() => "confirm"),
    ]);
    if (outcome === "blocked") {
      throw new Error(`${name} is unavailable on developer instances`);
    }
    if (await confirm.isDisabled()) {
      throw new Error(`installation is blocked for application: ${scope}`);
    }
    await confirm.click();
  });
  await step("wait-progress", async () => {
    await page.getByText(COMPLETE).first()
      .waitFor({ state: "visible", timeout: Math.max(timeout, 900_000) });
  });
  return { scope, installed: true };
}
