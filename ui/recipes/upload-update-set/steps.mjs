export async function run({ page, baseUrl, parameters, step, timeout }) {
  await step("open-upload", () =>
    page.goto(
      `${baseUrl}/upload.do?sysparm_referring_url=sys_remote_update_set_list.do` +
        "&sysparm_target=sys_remote_update_set",
      { waitUntil: "domcontentloaded" },
    ),
  );
  await step("attach-file", () =>
    page.locator('input[type="file"]').first().setInputFiles(parameters.file),
  );
  await step("submit", async () => {
    await Promise.all([
      page.waitForURL(/sys_remote_update_set/, { timeout }),
      page.locator('input[type="submit"], button[type="submit"]').first().click(),
    ]);
  });
  const url = new URL(page.url());
  return { uploaded: true, landing_page: url.pathname };
}
