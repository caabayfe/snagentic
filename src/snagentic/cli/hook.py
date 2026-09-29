"""Copilot CLI ``preToolUse`` hook: the Copilot-layer ServiceNow standards gate.

Installed with the plugin (``hooks.json``) as ``snagentic copilot hook pre-tool-use``.
It reads the hook payload on stdin and, for ``snagentic_instance_apply``, re-plans the
local changes and denies the call when the plan changed or the standards gate did not
pass. Anything else produces no output, which keeps Copilot's normal permission flow
(the user still approves apply). Errors deny: the gate fails closed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

GATED_TOOLS = frozenset({"snagentic_instance_apply"})


def _deny(reason: str) -> dict[str, str]:
    return {"permissionDecision": "deny", "permissionDecisionReason": reason[:2_000]}


def _repository_root(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        if (candidate / "instances").is_dir():
            return candidate
    return None


def _tool_arguments(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("toolArgs", payload.get("tool_input"))
    if isinstance(raw, str):
        raw = json.loads(raw) if raw.strip() else {}
    if not isinstance(raw, dict):
        raise ValueError("tool arguments are not an object")
    return raw


def pre_tool_use(text: str) -> dict[str, str] | None:
    try:
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("hook payload is not an object")
    except ValueError as exc:
        return _deny(f"snagentic gate received an unreadable hook payload: {exc}")
    tool = payload.get("toolName", payload.get("tool_name"))
    if tool not in GATED_TOOLS:
        return None
    try:
        return _apply_decision(payload)
    except Exception as exc:  # noqa: BLE001 - fail closed with a readable reason
        return _deny(f"snagentic standards gate could not evaluate the plan: {exc}")


def _apply_decision(payload: dict[str, Any]) -> dict[str, str] | None:
    from snagentic.instance.changes import ChangePlanner
    from snagentic.instance.config import InstanceRegistry
    from snagentic.instance.mirror import tune_large_repository
    from snagentic.review.gate import WAIVERS_FILE, denial_message

    arguments = _tool_arguments(payload)
    plan_id = arguments.get("planId")
    if not isinstance(plan_id, str):
        return _deny("snagentic_instance_apply needs the planId returned by plan")
    root = _repository_root(Path(str(payload.get("cwd") or ".")).resolve())
    if root is None:
        return _deny("cannot find the snagentic repository (instances/) from the session cwd")
    registry = InstanceRegistry(root)
    config = registry.load(arguments.get("instance") or None)
    paths = registry.paths(config.name)
    tune_large_repository(root)
    plan = ChangePlanner(paths, config).plan()
    if plan["plan_id"] != plan_id:
        return _deny(f"plan {plan_id} is stale (current plan is {plan['plan_id']}); "
                     "run snagentic_instance_plan and get the new plan approved")
    gate = plan["gate"]
    if gate["enforced"] and not gate["passed"]:
        return _deny(denial_message(
            gate, f"{paths.relative_workspace.as_posix()}/{WAIVERS_FILE}"
        ))
    return None
