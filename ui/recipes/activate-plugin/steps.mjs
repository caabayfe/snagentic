const ACTIVATE = /^\s*(Activate|Activate\/Upgrade|Install)\s*$/i;

export async function run({ page, baseUrl, parameters, step, timeout }) {
  const id = parameters.plugin_id;
  await step("open-plugin", async () => {
    await page.goto(
      `${baseUrl}/v_plugin_list.do?sysparm_query=id=${encodeURIComponent(id)}`,
      { waitUntil: "domcontentloaded" },
    );
    const link = page.locator("table.list_table a.linked.formlink").first();
    if ((await link.count()) === 0) throw new Error(`plugin not found: ${id}`);
    await Promise.all([page.waitForLoadState("domcontentloaded"), link.click()]);
  });
  const status = await page.locator('[id="v_plugin.active"], [name="v_plugin.active"]')
    .first().inputValue().catch(() => "");
  if (/^active$/i.test(status)) return { plugin_id: id, already_active: true };

  await step("start-activation", async () => {
    const action = page.getByRole("link", { name: ACTIVATE })
      .or(page.getByRole("button", { name: ACTIVATE })).first();
    await action.click();
  });
  await step("confirm-dialog", async () => {
    const dialog = page.locator(".modal-dialog, [role=dialog]").last();
    await dialog.waitFor({ state: "visible" });
    const demo = dialog.locator('input[type="checkbox"]').first();
    if ((await demo.count()) > 0) {
      await demo.setChecked(parameters.load_demo_data === "true").catch(() => {});
    }
    await dialog.getByRole("button", { name: /Activate/i }).last().click();
  });
  await step("wait-progress", async () => {
    await page.getByText(/(Activation|Plugin activation).*(complete|succeeded)|Close & Reload/i)
      .first().waitFor({ state: "visible", timeout: Math.max(timeout, 600_000) });
  });
  return { plugin_id: id, activated: true };
}
