"""ServiceNow best-practice rules.

Rule IDs are stable and cited by the ServiceNow expert skills in ``copilot-plugin/skills/``.
Categories follow ServiceNow Instance Scan: security, performance, upgradability,
manageability and user experience.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator

from snagentic.review.engine import DESCRIBED_TABLES, Hit, ReviewTarget, Rule, ScriptContext
from snagentic.review.lexer import Token

SERVER = frozenset({"server"})
CLIENT = frozenset({"client"})
BROWSER = frozenset({"client", "portal_client"})
ANY_SCRIPT = frozenset({"server", "client", "portal_client"})

SYS_ID_LITERAL = re.compile(r"^[0-9a-f]{32}$")
INSTANCE_URL = re.compile(
    r"https?://(?!(?:www|hi|install|docs|developer|support|store|community|instance|"
    r"signon|nowlearning)\.)[\w-]+\.(?:service-now\.com|servicenowservices\.com)",
    re.IGNORECASE)
SECRET_NAME = re.compile(
    r"(?:^|_)(?:pass(?:word|wd)?|pwd|secret|client_?secret|api_?key|apikey|token|"
    r"access_?token|auth_?token|private_?key)$",
    re.IGNORECASE,
)
PROPERTY_LIKE = re.compile(r"^[a-z0-9_-]+(?:\.[a-z0-9_-]+)+$")
PLACEHOLDER = re.compile(r"^(?:|\*+|x+|changeme|password|secret|token|<.*>|\$\{.*\}|"
                         r"\{\{.*\}\})$", re.IGNORECASE)
GLIDE_QUERY_CLASSES = frozenset({"GlideRecord", "GlideRecordSecure", "GlideAggregate",
                                 "GlideQuery"})
DOM_GLOBALS = frozenset({"jQuery", "$j", "$$", "gel"})
UI_POLICY_METHODS = frozenset({"setMandatory", "setDisplay", "setVisible", "setReadOnly",
                               "setReadonly", "setDisabled"})


def _identifiers(context: ScriptContext) -> Iterator[tuple[int, Token]]:
    for index, token in enumerate(context.script.tokens):
        if token.kind == "ident":
            yield index, token


def _member(context: ScriptContext, index: int, receiver: str, method: str) -> bool:
    return (context.script.is_member_call(index, method)
            and context.script.receiver(index) == receiver
            and not (index >= 3 and context.script.tokens[index - 3].value in {".", "?."}))


def _new_class(context: ScriptContext, index: int, names: Iterable[str]) -> bool:
    """``new Name`` or ``new namespace.Name`` with ``Name`` at ``index``."""

    tokens = context.script.tokens
    if tokens[index].value not in set(names) or index == 0:
        return False
    if tokens[index - 1].kind == "ident" and tokens[index - 1].value == "new":
        return True
    return (index >= 3 and tokens[index - 1].value == "." and tokens[index - 2].kind == "ident"
            and tokens[index - 3].value == "new")


# --- security ---------------------------------------------------------------------------


def check_eval(context: ScriptContext) -> Iterator[Hit]:
    for index, token in _identifiers(context):
        if context.script.is_call(index, "eval"):
            yield Hit(token.line, "eval() executes arbitrary strings as code")
        elif _new_class(context, index, {"GlideEvaluator", "Function"}):
            yield Hit(token.line, f"new {token.value}(...) compiles strings into code")
        elif (token.value == "GlideEvaluator" and index + 3 < len(context.script.tokens)
              and context.script.is_member_call(index + 2, "evaluateString")):
            yield Hit(token.line, "GlideEvaluator.evaluateString() evaluates strings as code")


def _literal_text(tokens: list[Token]) -> str | None:
    if len(tokens) == 1 and tokens[0].kind in {"string", "template"}:
        return tokens[0].value
    return None


def _looks_secret(value: str) -> bool:
    """Literal values that could be real secrets, not names/keys/messages that merely sit
    in a variable called ``token`` or ``password``."""

    if len(value) < 6 or PLACEHOLDER.match(value) or PROPERTY_LIKE.match(value):
        return False
    if any(char.isspace() for char in value):
        return False
    has_digit = any(char.isdigit() for char in value)
    has_alpha = any(char.isalpha() for char in value)
    has_symbol = any(not char.isalnum() and char not in "_-." for char in value)
    return has_alpha and (has_digit or has_symbol)


def check_hardcoded_credentials(context: ScriptContext) -> Iterator[Hit]:
    tokens = context.script.tokens
    for index, token in enumerate(tokens):
        # name = 'literal'  |  name: 'literal'  |  'name': 'literal'
        if (token.kind in {"ident", "string"} and SECRET_NAME.search(token.value)
                and index + 2 < len(tokens) and tokens[index + 1].value in {"=", ":"}):
            value = tokens[index + 2]
            follow = tokens[index + 3].value if index + 3 < len(tokens) else ";"
            if (value.kind == "string" and follow in {";", ",", "}", ")"}
                    and _looks_secret(value.value)):
                yield Hit(token.line, f"'{token.value}' is assigned a literal secret",
                          f"{token.value} = '***'")
        if context.script.is_member_call(index, "setBasicAuth"):
            args = context.script.arguments(index + 1)
            if len(args) == 2 and _literal_text(args[1]) not in (None, ""):
                yield Hit(token.line, "setBasicAuth() is called with a literal password",
                          "setBasicAuth(..., '***')")
        if context.script.is_member_call(index, "setRequestHeader"):
            args = context.script.arguments(index + 1)
            if len(args) == 2 and (_literal_text(args[0]) or "").lower() == "authorization":
                header = _literal_text(args[1]) or ""
                if re.match(r"(?i)(basic|bearer)\s+\S{8,}", header):
                    yield Hit(token.line, "an Authorization header value is hardcoded",
                              "setRequestHeader('Authorization', '***')")


def check_encoded_query_concatenation(context: ScriptContext) -> Iterator[Hit]:
    tokens = context.script.tokens
    for index, token in enumerate(tokens):
        if not context.script.is_member_call(index, "addEncodedQuery"):
            continue
        args = context.script.arguments(index + 1)
        if not args:
            continue
        arg = args[0]
        concatenated = any(t.kind == "punct" and t.value in {"+", "+="} for t in arg)
        interpolated = any(t.kind == "template" and "${" in t.value for t in arg)
        dynamic = any(t.kind == "ident" for t in arg)
        if (concatenated and dynamic) or interpolated:
            yield Hit(token.line, "encoded query is built by string concatenation; values "
                      "containing '^' or 'OR' change the query")


def check_acl_script_grants_all(context: ScriptContext) -> Iterator[Hit]:
    code = [t.value for t in context.script.tokens]
    trivial = (
        ["answer", "=", "true"], ["answer", "=", "true", ";"],
        ["true"], ["true", ";"], ["return", "true"], ["return", "true", ";"],
    )
    if code in [list(option) for option in trivial]:
        yield Hit(1, "the ACL script always evaluates to true")


def check_packages_calls(context: ScriptContext) -> Iterator[Hit]:
    tokens = context.script.tokens
    for index, token in enumerate(tokens):
        if (token.kind == "ident" and token.value == "Packages" and index + 1 < len(tokens)
                and tokens[index + 1].value == "."
                and not (index > 0 and tokens[index - 1].value == ".")):
            yield Hit(token.line, "Packages.* calls Java classes directly")


# --- performance ------------------------------------------------------------------------


def check_current_update(context: ScriptContext) -> Iterator[Hit]:
    when = context.values.get("when", "")
    if when not in {"before", "after"}:
        return
    for index, token in _identifiers(context):
        if _member(context, index, "current", "update"):
            reason = ("the record is saved anyway after a before rule"
                      if when == "before" else "it re-runs every before/after rule and can "
                      "recurse")
            yield Hit(token.line, f"current.update() in a {when} business rule: {reason}")


def _loop_bodies(context: ScriptContext) -> Iterator[tuple[int, int]]:
    """Token ranges of ``while (x.next()) { ... }`` and ``for``/``forEach`` loop bodies."""

    tokens = context.script.tokens
    script = context.script
    for index, token in enumerate(tokens):
        if token.kind != "ident":
            continue
        if token.value in {"while", "for"} and index + 1 < len(tokens) and (
                tokens[index + 1].value == "("):
            close = script.match(index + 1)
            if close is None or close + 1 >= len(tokens):
                continue
            header = tokens[index + 2:close]
            if token.value == "while" and not any(t.value == "next" for t in header):
                continue
            if tokens[close + 1].value == "{":
                end = script.match(close + 1)
                if end is not None:
                    yield close + 1, end
        elif script.is_member_call(index, "forEach"):
            close = script.match(index + 1)
            if close is not None:
                yield index + 1, close


def check_query_in_loop(context: ScriptContext) -> Iterator[Hit]:
    reported: set[int] = set()
    for start, end in _loop_bodies(context):
        for index in range(start, end):
            token = context.script.tokens[index]
            if (index not in reported and token.kind == "ident"
                    and _new_class(context, index, GLIDE_QUERY_CLASSES)):
                reported.add(index)
                yield Hit(token.line, f"new {token.value}() inside a loop runs one query per "
                          "iteration")


def check_get_row_count(context: ScriptContext) -> Iterator[Hit]:
    for index, token in _identifiers(context):
        if context.script.is_member_call(index, "getRowCount"):
            yield Hit(token.line, "getRowCount() fetches every matching row to count them")


def check_sleep(context: ScriptContext) -> Iterator[Hit]:
    for index, token in _identifiers(context):
        if _member(context, index, "gs", "sleep"):
            yield Hit(token.line, "gs.sleep() blocks a worker thread")


def check_client_glide_record(context: ScriptContext) -> Iterator[Hit]:
    for index, token in _identifiers(context):
        if _new_class(context, index, {"GlideRecord"}):
            yield Hit(token.line, "GlideRecord in a client script makes a synchronous server "
                      "round trip and exposes table access to the browser")


def check_sync_client_calls(context: ScriptContext) -> Iterator[Hit]:
    for index, token in _identifiers(context):
        script = context.script
        if script.is_member_call(index, "getXMLWait"):
            yield Hit(token.line, "getXMLWait() blocks the browser until the server responds")
        elif script.is_member_call(index, "getReference") and len(
                script.arguments(index + 1)) < 2:
            yield Hit(token.line, "g_form.getReference() without a callback is synchronous")


def check_outbound_in_before_rule(context: ScriptContext) -> Iterator[Hit]:
    if context.values.get("when", "") not in {"before", "display"}:
        return
    for index, token in _identifiers(context):
        if _new_class(context, index, {"RESTMessageV2", "SOAPMessageV2", "GlideHTTPRequest"}):
            yield Hit(token.line, f"{token.value} in a {context.values.get('when')} business "
                      "rule holds the user's transaction open for the remote call")


# --- upgradability ----------------------------------------------------------------------


def check_dom_access(context: ScriptContext) -> Iterator[Hit]:
    tokens = context.script.tokens
    for index, token in enumerate(tokens):
        if token.kind != "ident" or (index > 0 and tokens[index - 1].value in {".", "?."}):
            continue
        following = tokens[index + 1].value if index + 1 < len(tokens) else ""
        if token.value == "document" and following == ".":
            yield Hit(token.line, "direct document access depends on form HTML that changes "
                      "between releases")
        elif token.value in DOM_GLOBALS | {"$"} and following == "(":
            yield Hit(token.line, f"{token.value}(...) manipulates the DOM directly")


def check_baseline_customization(target: ReviewTarget) -> Iterator[Hit]:
    if target.operation == "update" and target.customized is False:
        yield Hit(None, "this change customizes an out-of-box record, which will be skipped "
                  "or need review on every upgrade")


# --- manageability ----------------------------------------------------------------------


def check_hardcoded_sys_id(context: ScriptContext) -> Iterator[Hit]:
    for token in context.script.tokens:
        if token.kind == "string" and SYS_ID_LITERAL.match(token.value):
            yield Hit(token.line, "a sys_id is hardcoded; it differs between instances and "
                      "breaks when the record is recreated")


def check_hardcoded_instance_url(context: ScriptContext) -> Iterator[Hit]:
    for token in context.script.tokens:
        if token.kind in {"string", "template"} and INSTANCE_URL.search(token.value):
            yield Hit(token.line, "an instance URL is hardcoded and will point at the wrong "
                      "instance after clone or promotion")


def check_scoped_gs_log(context: ScriptContext) -> Iterator[Hit]:
    if not context.scoped:
        return
    for index, token in _identifiers(context):
        if _member(context, index, "gs", "log") or _member(context, index, "gs", "print"):
            method = context.script.tokens[index].value
            yield Hit(token.line, f"gs.{method}() is not available in scoped applications")


def check_set_workflow_false(context: ScriptContext) -> Iterator[Hit]:
    tokens = context.script.tokens
    for index, token in _identifiers(context):
        if (context.script.is_member_call(index, "setWorkflow")
                and index + 3 < len(tokens) and tokens[index + 2].value == "false"):
            yield Hit(token.line, "setWorkflow(false) skips business rules, auditing and "
                      "notifications for this write")


def check_script_include_name(context: ScriptContext) -> Iterator[Hit]:
    name = context.values.get("name", "").strip()
    if not name:
        return
    tokens = context.script.tokens
    defined: list[Token] = []
    for index, token in enumerate(tokens[:-3]):
        if (token.kind == "ident" and tokens[index + 1].value == "="
                and tokens[index + 2].value == "Class" and tokens[index + 3].value == "."):
            defined.append(token)
    if defined and all(token.value != name for token in defined):
        yield Hit(defined[0].line, f"the class is '{defined[0].value}' but the script "
                  f"include is named '{name}'; callers cannot resolve it")


def check_missing_description(target: ReviewTarget) -> Iterator[Hit]:
    if target.operation == "create" and not (target.values.get("description") or "").strip():
        yield Hit(None, "new script records should describe their purpose")


# --- user experience --------------------------------------------------------------------


def check_on_change_is_loading(context: ScriptContext) -> Iterator[Hit]:
    if context.values.get("type") != "onChange":
        return
    if not any(t.kind == "ident" and t.value == "isLoading" for t in context.script.tokens):
        yield Hit(1, "onChange script runs on form load too; return early when isLoading")


def check_ui_policy_candidate(context: ScriptContext) -> Iterator[Hit]:
    if context.values.get("type") not in {"onLoad", "onChange"}:
        return
    tokens = context.script.tokens
    if any(t.kind == "ident" and t.value in {"GlideAjax", "getReference", "GlideRecord"}
           for t in tokens):
        return
    g_form_calls: set[str] = set()
    for index, token in _identifiers(context):
        if (token.value == "g_form" and index + 2 < len(tokens)
                and tokens[index + 1].value == "." and tokens[index + 2].kind == "ident"):
            g_form_calls.add(tokens[index + 2].value)
    if (g_form_calls & UI_POLICY_METHODS) and g_form_calls <= UI_POLICY_METHODS | {"getValue"}:
        yield Hit(1, "this script only changes mandatory/visible/read-only state; a UI policy "
                  "does that declaratively and also applies to lists and mobile")


def check_alert(context: ScriptContext) -> Iterator[Hit]:
    for index, token in _identifiers(context):
        if context.script.is_call(index, "alert") or context.script.is_call(index, "confirm"):
            yield Hit(token.line, f"{token.value}() is a blocking browser dialog")


RULES: tuple[Rule, ...] = (
    Rule("SN-SEC-001", "security", "block", "No dynamic code evaluation",
         "eval, GlideEvaluator and new Function run strings as code, turning any "
         "attacker-controlled value into code execution.",
         "Call the intended function directly, or map allowed inputs to handlers.",
         ANY_SCRIPT, script_check=check_eval),
    Rule("SN-SEC-002", "security", "block", "No credentials in scripts",
         "Secrets in scripts end up in update sets, clones, source control and XML exports.",
         "Use a Connection & Credential alias, a credential record or a password2 field.",
         ANY_SCRIPT, script_check=check_hardcoded_credentials, redact_evidence=True),
    Rule("SN-SEC-003", "security", "warn", "Do not concatenate encoded queries",
         "Values containing '^', '^OR' or '^NQ' change an encoded query and can widen "
         "the result set (query injection).",
         "Use addQuery(field, operator, value) per condition, or GlideQuery.",
         SERVER, script_check=check_encoded_query_concatenation),
    Rule("SN-SEC-004", "security", "warn", "ACL scripts must not grant unconditionally",
         "An ACL whose script always returns true adds no protection: access depends only "
         "on roles and conditions, and on nothing at all when neither is set.",
         "Remove the script and rely on roles/conditions, or test a real condition.",
         SERVER, frozenset({"sys_security_acl"}), script_check=check_acl_script_grants_all),
    Rule("SN-SEC-005", "security", "block", "No Packages.* Java calls",
         "Packages calls bypass the platform API contract, are blocked in scoped apps and "
         "break on upgrade.",
         "Use the documented Glide API equivalent.",
         SERVER, script_check=check_packages_calls),
    Rule("SN-PERF-001", "performance", "block",
         "No current.update() in before/after business rules",
         "Before rules save current automatically; in after rules update() triggers the "
         "whole rule chain again and can recurse.",
         "Set fields on current in a before rule. For after rules, update other records "
         "or use an async rule.",
         SERVER, frozenset({"sys_script"}), script_check=check_current_update),
    Rule("SN-PERF-002", "performance", "warn", "No queries inside loops",
         "One query per iteration (N+1) multiplies database round trips.",
         "Query once with an IN condition, use GlideAggregate, or a join (addJoinQuery).",
         SERVER, script_check=check_query_in_loop),
    Rule("SN-PERF-003", "performance", "warn", "Avoid getRowCount()",
         "getRowCount() retrieves all matching rows to count them.",
         "Use GlideAggregate with COUNT, or setLimit(1) and hasNext() for existence.",
         SERVER, script_check=check_get_row_count),
    Rule("SN-PERF-004", "performance", "warn", "No gs.sleep()",
         "Sleeping holds a scarce worker or semaphore thread.",
         "Schedule the follow-up work (scheduled job, event, flow wait).",
         SERVER, script_check=check_sleep),
    Rule("SN-PERF-005", "performance", "block", "No GlideRecord in client scripts",
         "Client GlideRecord makes synchronous calls and exposes whole records to the "
         "browser.",
         "Use GlideAjax with an asynchronous callback, g_scratchpad from a display rule, "
         "or a UI policy.",
         CLIENT, script_check=check_client_glide_record),
    Rule("SN-PERF-006", "performance", "warn", "No synchronous client calls",
         "getXMLWait() and getReference() without a callback freeze the form.",
         "Use getXMLAnswer()/getXML() or getReference(field, callback).",
         BROWSER, script_check=check_sync_client_calls),
    Rule("SN-PERF-007", "performance", "warn",
         "No outbound calls in before/display business rules",
         "A synchronous integration call holds the user's transaction open and fails the "
         "save when the endpoint is slow.",
         "Use an async business rule, an event and script action, or a Flow/IntegrationHub "
         "action.",
         SERVER, frozenset({"sys_script"}), script_check=check_outbound_in_before_rule),
    Rule("SN-UPG-001", "upgradability", "block", "No DOM manipulation in client scripts",
         "Form HTML is not an API; direct DOM/jQuery access breaks on upgrade and in "
         "Workspaces.",
         "Use the g_form/g_list/GlideModal APIs or a UI policy.",
         CLIENT, script_check=check_dom_access),
    Rule("SN-UPG-002", "upgradability", "info", "Prefer not to modify out-of-box records",
         "Customized baseline records are skipped on upgrade and must be reconciled.",
         "Deactivate and copy, extend, or add a new record with a higher order instead.",
         record_check=check_baseline_customization),
    Rule("SN-MNT-001", "manageability", "warn", "No hardcoded sys_ids",
         "sys_ids differ across instances for data records and make intent unreadable.",
         "Use a system property, a lookup by a stable key, or a reference field.",
         ANY_SCRIPT, script_check=check_hardcoded_sys_id),
    Rule("SN-MNT-002", "manageability", "warn", "No hardcoded instance URLs",
         "Hardcoded URLs point at the wrong instance after clones and promotion.",
         "Use gs.getProperty('glide.servlet.uri') or a system property.",
         ANY_SCRIPT, script_check=check_hardcoded_instance_url),
    Rule("SN-MNT-003", "manageability", "warn", "Use gs.info/warn/error in scoped apps",
         "gs.log() and gs.print() are unavailable in scoped applications.",
         "Use gs.info(), gs.warn(), gs.error() or gs.debug().",
         SERVER, script_check=check_scoped_gs_log),
    Rule("SN-MNT-004", "manageability", "warn", "Avoid setWorkflow(false)",
         "Skipping the engine hides writes from audit, notifications and other rules.",
         "Remove it, or document why every downstream rule must be skipped.",
         SERVER, script_check=check_set_workflow_false),
    Rule("SN-MNT-005", "manageability", "warn", "Script include name must match its class",
         "The platform resolves script includes by record name; a mismatched class is "
         "unreachable.",
         "Rename the class or the record so they match.",
         SERVER, frozenset({"sys_script_include"}), script_check=check_script_include_name),
    Rule("SN-MNT-006", "manageability", "info", "Describe new scripts",
         "A description explains intent to the next maintainer and to Instance Scan.",
         "Fill in the description field.",
         tables=DESCRIBED_TABLES, record_check=check_missing_description),
    Rule("SN-UX-001", "user_experience", "warn", "Guard onChange scripts with isLoading",
         "onChange scripts also fire while the form loads, causing flicker and extra "
         "server calls.",
         "Start with: if (isLoading || newValue === '') { return; }",
         CLIENT, frozenset({"sys_script_client", "catalog_script_client"}),
         script_check=check_on_change_is_loading),
    Rule("SN-UX-002", "user_experience", "info", "Prefer UI policies for field state",
         "UI policies are declarative, ordered, reversible and faster to maintain.",
         "Replace the script with a UI policy and UI policy actions.",
         CLIENT, frozenset({"sys_script_client", "catalog_script_client"}),
         script_check=check_ui_policy_candidate),
    Rule("SN-UX-003", "user_experience", "info", "No alert()/confirm() dialogs",
         "Browser dialogs block the page and do not work in Workspaces or mobile.",
         "Use g_form.addErrorMessage/showFieldMsg or GlideModal.",
         BROWSER, script_check=check_alert),
)


def rule_catalog() -> list[dict[str, object]]:
    return [rule.describe() for rule in RULES]
