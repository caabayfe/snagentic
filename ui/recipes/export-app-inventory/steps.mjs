const TILE = "sn-app-mgr-tile";
const NEXT = /next/i;

async function readTiles(page) {
  return page.locator(TILE).evaluateAll((elements) =>
    elements.map((element, index) => {
      const descendants = (root) => {
        const found = [];
        for (const child of root?.children ?? []) {
          found.push(child, ...descendants(child.shadowRoot), ...descendants(child));
        }
        return found;
      };
      const nodes = [element, ...descendants(element.shadowRoot), ...descendants(element)];
      const attributes = Object.fromEntries(
        [...element.attributes].map((attribute) => [attribute.name, attribute.value]),
      );
      const links = nodes.filter((node) => node.localName === "a").map((link) => ({
        href: link.href,
        text: link.innerText.replace(/\s+/g, " ").trim(),
      }));
      const sections = [];
      let parent = element;
      while (parent && sections.length < 4) {
        parent = parent.parentElement ?? parent.getRootNode()?.host;
        if (!parent) break;
        if (parent.localName?.startsWith("sn-app-mgr-")) sections.push(parent.localName);
      }
      return {
        index,
        name: element.name ?? attributes.name ?? "",
        scope: element.scope ?? "",
        sys_id: element.sysId ?? "",
        version: element.latestVersion ?? "",
        vendor: element.vendor ?? "",
        short_description: element.shortDescription ?? "",
        attributes,
        links,
        sections,
      };
    }),
  );
}

async function collectPages(page, collected, expected, timeout) {
  for (let pageNumber = 1; pageNumber <= 300; pageNumber += 1) {
    const tiles = await readTiles(page);
    for (const tile of tiles) {
      const href = tile.links.find((link) => link.href)?.href ?? "";
      const key = tile.sys_id || tile.scope || href || tile.attributes["sys-id"] ||
        tile.attributes.id || tile.name || `${pageNumber}:${tile.index}`;
      collected.set(key, { ...tile, page: pageNumber });
    }
    if (collected.size >= expected) return true;
    const pagination = page.locator(
      'sn-app-mgr-custom-pagination[type="applications"]',
    );
    const next = pagination.getByRole("button", { name: NEXT }).last()
      .or(pagination.locator('button[aria-label*="next" i]').last());
    if (await next.count() === 0 || await next.isDisabled().catch(() => true)) return true;
    const before = tiles[0]?.sys_id || tiles[0]?.scope || tiles[0]?.name;
    await next.click();
    const deadline = Date.now() + Math.min(timeout, 10_000);
    let identity;
    while (Date.now() < deadline) {
      await page.waitForTimeout(100);
      const current = (await readTiles(page))[0];
      identity = current?.sys_id || current?.scope || current?.name;
      if (identity && identity !== before) break;
    }
    if (!identity || identity === before) return false;
    await page.waitForTimeout(300);
  }
  return false;
}

export async function run({ page, baseUrl, step, timeout }) {
  await step("open-application-manager", () =>
    page.goto(`${baseUrl}/now/app-manager/home`, { waitUntil: "domcontentloaded" }),
  );
  await step("wait-for-inventory", async () => {
    await page.locator(TILE).first().waitFor({ state: "visible", timeout });
    await page.waitForTimeout(2_000);
  });
  await step("open-complete-application-list", async () => {
    const more = page.getByRole("button", { name: /View more Store applications/i });
    await more.waitFor({ state: "visible", timeout });
    await more.click();
    await page.locator(TILE).first().waitFor({ state: "visible", timeout });
    await page.waitForTimeout(1_000);
  });
  await step("maximize-page-size", async () => {
    const pagination = page.locator(
      'sn-app-mgr-custom-pagination[type="applications"]',
    );
    const dropdown = pagination.getByRole("combobox");
    await dropdown.click();
    const options = page.getByRole("option");
    await options.first().waitFor({ state: "visible", timeout });
    const values = (await options.allTextContents())
      .map((value) => Number(value.trim()))
      .filter(Number.isFinite);
    if (values.length === 0) throw new Error("Application Manager page sizes are unavailable");
    await page.getByRole(
      "option", { name: String(Math.max(...values)), exact: true },
    ).last().click({ force: true });
    await page.waitForTimeout(1_000);
  });
  const records = await step("collect-inventory", async () => {
    const collected = new Map();
    const heading = await page.getByText(/Store applications \(\d+\)/).first().innerText();
    const expected = Number(/\((\d+)\)/.exec(heading)?.[1] ?? 0);
    if (!expected) throw new Error("Application Manager did not report an application count");
    let complete = await collectPages(page, collected, expected, timeout);
    if (!complete) {
      const pagination = page.locator(
        'sn-app-mgr-custom-pagination[type="applications"]',
      );
      await pagination.getByRole("button", { name: "Last page", exact: true })
        .click({ force: true });
      await page.waitForTimeout(1_000);
      for (const tile of await readTiles(page)) {
        const key = tile.sys_id || tile.scope || tile.name;
        if (key) collected.set(key, tile);
      }
      complete = true;
    }
    return {
      complete,
      displayed_count: expected,
      duplicate_count: Math.max(0, expected - collected.size),
      records: [...collected.values()].map((record) => ({
        sys_id: record.sys_id,
        scope: record.scope.replace(/^App id:\s*/i, ""),
        name: record.name,
        version: record.version,
        vendor: record.vendor,
        short_description: record.short_description,
      })),
    };
  });
  if (records.records.length === 0) {
    throw new Error("Application Manager returned no inventory tiles");
  }
  return records;
}
