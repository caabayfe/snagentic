---
name: servicenow-client-ux
description: ServiceNow client-side and UX standards for client scripts, catalog client scripts, UI policies, client UI actions, UI scripts and Service Portal widgets. Use when changing form behaviour, field visibility/mandatory/read-only state, or browser-side ServiceNow code.
---

# ServiceNow client scripting and UX

## Choose the right tool

| Need | Use |
|------|-----|
| Mandatory / visible / read-only depending on field values | **UI policy** (SN-UX-002) |
| Mandatory for every channel (forms, imports, APIs) | **Data policy** |
| Default values | Dictionary default or template |
| Filter choices of a reference | Reference qualifier |
| Server data on load | Display business rule → `g_scratchpad` |
| Server data on change | `GlideAjax` + `getXMLAnswer(callback)` |
| Messages | `g_form.showFieldMsg`, `addErrorMessage`, `GlideModal` |

## Rules

- No `GlideRecord` in the browser (SN-PERF-005): use `GlideAjax` against a
  client-callable script include, or `g_scratchpad`.
- No synchronous calls (SN-PERF-006): `getXMLAnswer`/`getXML` with a callback;
  `g_form.getReference(field, callback)`.
- No DOM, jQuery, Prototype `$()`, `gel()` or `document.*` (SN-UPG-001). Only the
  `g_form`, `g_user`, `g_list`, `GlideModal` APIs are stable across releases and
  Workspaces. Set **Isolate script** to true on new client scripts.
- onChange scripts start with (SN-UX-001):
  ```js
  function onChange(control, oldValue, newValue, isLoading, isTemplate) {
      if (isLoading || newValue === '') {
          return;
      }
      ...
  }
  ```
- No `alert()`/`confirm()` (SN-UX-003); use `g_form.addErrorMessage` or `GlideModal`.
- Set `UI type` deliberately (Desktop, Mobile / Service Portal, All) and test in the
  Workspace if the table is used there.
- onSubmit scripts return `false` only after telling the user why.
- Translate user-visible text with `getMessage()`.

## Client UI actions

- Put client code in a function referenced by `onclick`, then call
  `gsftSubmit(null, g_form.getFormElement(), '<action_name>')` to run the server part.
- Guard the server part with `if (typeof window == 'undefined') serverSide();`.

## Service Portal widgets

- Server script: fill `data`, validate `input`, use `GlideRecordSecure`.
- Client controller: use `c.server.get/update` and Angular services; no direct DOM.
- Templates: bind with `{{ }}` (escaped); avoid `ng-bind-html` with user data.
