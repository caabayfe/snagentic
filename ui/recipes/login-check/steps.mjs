export async function run({ page, baseUrl, step }) {
  const text = await step("stats", async () => {
    await page.goto(`${baseUrl}/stats.do`, { waitUntil: "domcontentloaded" });
    return page.locator("body").innerText();
  });
  const read = (label) => {
    const match = text.match(new RegExp(`${label}:\\s*([^\\n]+)`));
    return match ? match[1].trim() : null;
  };
  return {
    logged_in: true,
    build_name: read("Build name"),
    build_tag: read("Build tag"),
    build_date: read("Build date"),
  };
}
