const INCIDENT_ID = /^[0-9a-f]{32}$/;

async function openIncident({ page, baseUrl, parameters, classicFrame }) {
  if (parameters.incident_sys_id) {
    await page.goto(`${baseUrl}/incident.do?sys_id=${parameters.incident_sys_id}`, {
      waitUntil: "domcontentloaded",
    });
    return parameters.incident_sys_id;
  }

  const active = parameters.scenario === "inactive" ? "false" : "true";
  await page.goto(
    `${baseUrl}/incident_list.do?sysparm_query=active=${active}` +
      "^ORDERBYDESCsys_updated_on&sysparm_limit=20",
    { waitUntil: "domcontentloaded" },
  );
  const frame = classicFrame();
  const link = frame.locator('a.linked.formlink[href*="incident.do?sys_id="]').first();
  if ((await link.count()) === 0) {
    throw new Error(`no ${active === "true" ? "active" : "inactive"} Incident record is available`);
  }
  const href = await link.getAttribute("href");
  const match = href?.match(/[?&]sys_id=([0-9a-f]{32})/);
  if (!match) throw new Error("could not read the selected Incident sys_id");
  await Promise.all([page.waitForLoadState("domcontentloaded"), link.click()]);
  return match[1];
}

export async function run({ page, baseUrl, parameters, step, classicFrame }) {
  const scenario = parameters.scenario ?? "save";
  const incidentSysId = await step("open-incident", () =>
    openIncident({ page, baseUrl, parameters, classicFrame }));

  if (scenario === "inactive") {
    return step("verify-action-hidden", async () => {
      const frame = classicFrame();
      const action = frame.getByRole("button", {
        name: "Create Incident Task",
        exact: true,
      }).or(frame.getByRole("link", {
        name: "Create Incident Task",
        exact: true,
      }));
      if ((await action.count()) !== 0) {
        throw new Error("Create Incident Task is visible on an inactive Incident");
      }
      return { incident_sys_id: incidentSysId, action_visible: false };
    });
  }

  await step("create-incident-task", async () => {
    const frame = classicFrame();
    const action = frame.getByRole("button", { name: "Create Incident Task", exact: true })
      .or(frame.getByRole("link", { name: "Create Incident Task", exact: true }))
      .first();
    if ((await action.count()) === 0) {
      throw new Error("Create Incident Task is not visible on the Incident form");
    }
    await Promise.all([
      page.waitForURL(/incident_task\.do/u),
      action.click(),
    ]);
  });

  const proposed = await step("verify-proposed-task", async () => {
    const frame = classicFrame();
    const incident = frame.locator("#incident_task\\.incident");
    await incident.waitFor({ state: "attached" });
    const linkedIncident = await incident.inputValue();
    if (linkedIncident !== incidentSysId) {
      throw new Error(`Incident Task is linked to ${linkedIncident || "nothing"}, not ${incidentSysId}`);
    }
    const shortDescription = frame.locator("#incident_task\\.short_description");
    const inheritedDescription = await shortDescription.inputValue();
    const validationDescription =
      parameters.short_description ?? `snagentic incident task validation ${Date.now()}`;
    await shortDescription.fill(validationDescription);
    return {
      incident_sys_id: linkedIncident,
      inherited_short_description: inheritedDescription,
      short_description: validationDescription,
    };
  });

  const persisted = await step(
    scenario === "cancel" ? "cancel-incident-task" : "save-incident-task",
    async () => {
    const frame = classicFrame();
    const number = frame.locator("#incident_task\\.number");
    await number.waitFor({ state: "attached" });
    const expectedNumber = await number.inputValue();
    if (scenario === "cancel") {
      await page.goto(`${baseUrl}/incident.do?sys_id=${incidentSysId}`, {
        waitUntil: "domcontentloaded",
      });
      await page.goto(
        `${baseUrl}/incident_task_list.do?sysparm_query=number=` +
          `${encodeURIComponent(expectedNumber)}&sysparm_limit=1`,
        { waitUntil: "domcontentloaded" },
      );
      const savedLink = classicFrame()
        .locator('a.linked.formlink[href*="incident_task.do?sys_id="]')
        .first();
      if ((await savedLink.count()) !== 0) {
        throw new Error("Cancelled Incident Task was persisted");
      }
      return { number: expectedNumber, persisted: false };
    }
    const insertAndStay = frame.locator("#sysverb_insert_and_stay");
    const insert = frame.locator("#sysverb_insert");
    const save = (await insertAndStay.count()) > 0 ? insertAndStay : insert;
    if ((await save.count()) === 0) throw new Error("Incident Task insert action is unavailable");
    await save.click();
    await page.goto(
      `${baseUrl}/incident_task_list.do?sysparm_query=number=${encodeURIComponent(expectedNumber)}` +
        "&sysparm_limit=1",
      { waitUntil: "domcontentloaded" },
    );
    const listFrame = classicFrame();
    const savedLink = listFrame.locator('a.linked.formlink[href*="incident_task.do?sys_id="]').first();
    if ((await savedLink.count()) === 0) throw new Error("Incident Task was not saved");
    const href = await savedLink.getAttribute("href");
    const match = href?.match(/[?&]sys_id=([0-9a-f]{32})/);
    if (!match || !INCIDENT_ID.test(match[1])) {
      throw new Error("Saved Incident Task has no sys_id");
    }
    const sysId = match[1];
    await page.goto(`${baseUrl}/incident_task.do?sys_id=${sysId}`, {
      waitUntil: "domcontentloaded",
    });
    const savedFrame = classicFrame();
    const savedNumber = savedFrame.locator("#incident_task\\.number");
    await savedNumber.waitFor({ state: "attached" });
    const actualNumber = await savedNumber.inputValue();
    if (actualNumber !== expectedNumber) throw new Error("Incident Task was not saved");
    const savedDescription =
      await savedFrame.locator("#incident_task\\.short_description").inputValue();
    if (savedDescription !== proposed.short_description) {
      throw new Error("Incident Task short description was not saved");
    }
    return {
      number: actualNumber,
      persisted: true,
      sys_id: sysId,
    };
  });

  return scenario === "cancel"
    ? { proposed, cancelled: persisted }
    : { proposed, created: persisted };
}
