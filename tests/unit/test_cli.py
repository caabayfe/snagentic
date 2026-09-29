import json
from pathlib import Path

import pytest

from snagentic.cli.main import main


def test_doctor_without_local_runs_local_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    main(["--json", "doctor"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert "credential_store" in payload["result"]


@pytest.mark.parametrize(
    "command",
    [
        "init",
        "inventory",
        "pull",
        "status",
        "diff",
        "validate",
        "-".join(("push", "plan")),
        "push",
        "diagnostics",
        "query",
        "-".join(("promotion", "manifest")),
        "-".join(("change", "report")),
    ],
)
def test_legacy_subcommands_are_rejected(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--json", command])

    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
