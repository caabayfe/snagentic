from __future__ import annotations

import pytest
from conftest import Harness

from snagentic.errors import PolicyDeniedError, ServiceNowError
from snagentic.instance.ops import PlatformOperations, complete_update_set


def test_plugin_activation_polls_progress(harness: Harness) -> None:
    harness.fake.progress_statuses = ["1", "1", "2"]
    sleeps: list[float] = []
    ops = PlatformOperations(harness.config, harness.client, sleep=sleeps.append)
    result = ops.run("plugin.activate", {"plugin_id": "com.glide.hub"}, confirm=True)
    assert result["result"]["outcome"] == "successful"
    assert ("POST", "/api/sn_cicd/plugin/com.glide.hub/activate", {}) in harness.fake.requests
    assert len(sleeps) == 2


def test_operations_require_confirmation_and_development(harness: Harness) -> None:
    ops = PlatformOperations(harness.config, harness.client)
    with pytest.raises(PolicyDeniedError):
        ops.run("plugin.activate", {"plugin_id": "com.glide.hub"}, confirm=False)
    prod = PlatformOperations(harness.config.model_copy(update={"kind": "production"}),
                              harness.client)
    with pytest.raises(PolicyDeniedError):
        prod.run("plugin.activate", {"plugin_id": "com.glide.hub"}, confirm=True)
    assert prod.run("progress", {"progress_id": "p1"}, confirm=False)["result"]["status"] == "2"


def test_operation_parameter_validation(harness: Harness) -> None:
    ops = PlatformOperations(harness.config, harness.client)
    with pytest.raises(ValueError, match="unknown operation"):
        ops.run("plugin.delete", {}, confirm=True)
    with pytest.raises(ValueError, match="unsupported"):
        ops.run("plugin.activate", {"plugin_id": "x", "evil": "1"}, confirm=True)
    with pytest.raises(ValueError, match="unsafe"):
        ops.run("plugin.activate", {"plugin_id": "../table/sys_user"}, confirm=True)
    with pytest.raises(ValueError, match="missing"):
        ops.run("update_set.create", {}, confirm=True)


def test_failed_operation_raises(harness: Harness) -> None:
    harness.fake.progress_statuses = ["3"]
    ops = PlatformOperations(harness.config, harness.client, sleep=lambda _: None)
    with pytest.raises(ServiceNowError, match="failed"):
        ops.run("atf.run", {"test_suite_name": "Smoke"}, confirm=True)


def test_complete_update_set_only_for_agent_sets(harness: Harness) -> None:
    fake = harness.fake
    own = fake.insert("sys_update_set",
                      {"name": "snagentic: main [global]", "state": "in progress"})
    other = fake.insert("sys_update_set", {"name": "Alice work", "state": "in progress"})
    with pytest.raises(PolicyDeniedError):
        complete_update_set(harness.config, harness.client, own, confirm=False)
    with pytest.raises(PolicyDeniedError):
        complete_update_set(harness.config, harness.client, other, confirm=True)
    result = complete_update_set(harness.config, harness.client, own, confirm=True)
    assert result["state"] == "complete"
