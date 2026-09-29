import pytest

from snagentic.errors import PolicyDeniedError
from snagentic.policy import EnvironmentKind, PolicyEnforcer


def test_only_development_is_writable_by_default() -> None:
    enforcer = PolicyEnforcer()
    enforcer.require_write_allowed(EnvironmentKind.DEVELOPMENT)
    for environment in (EnvironmentKind.TEST, EnvironmentKind.PRODUCTION):
        with pytest.raises(PolicyDeniedError):
            enforcer.require_write_allowed(environment)


def test_sensitive_fields_are_removed_recursively() -> None:
    filtered = PolicyEnforcer().exclude_sensitive_fields(
        {
            "name": "safe",
            "password": "secret",
            "nested": {"Token": "secret", "description": "safe"},
            "records": [{"client_secret": "secret", "id": "one"}],
        }
    )
    assert filtered == {
        "name": "safe",
        "nested": {"description": "safe"},
        "records": [{"id": "one"}],
    }


def test_diagnostic_limits_are_enforced() -> None:
    enforcer = PolicyEnforcer()
    enforcer.validate_diagnostic_request(minutes=1, limit=5000)
    with pytest.raises(PolicyDeniedError):
        enforcer.validate_diagnostic_request(minutes=0, limit=1)
    with pytest.raises(PolicyDeniedError):
        enforcer.validate_diagnostic_request(minutes=1, limit=5001)
    with pytest.raises(PolicyDeniedError):
        enforcer.validate_diagnostic_request(
            minutes=1,
            limit=1,
            payload_bytes=enforcer.diagnostic_limits.max_payload_bytes + 1,
        )
