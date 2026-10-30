from __future__ import annotations

import io

import pytest

from app.bootstrap import run


def _raise_missing(name: str):
    def _loader():
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)

    return _loader


def test_missing_third_party_package_explains_the_venv():
    stderr = io.StringIO()

    code = run(
        ["--list-memories"],
        load_main=_raise_missing("yaml"),
        stderr=stderr,
    )

    assert code == 2
    message = stderr.getvalue()
    assert "yaml" in message
    assert ".venv\\Scripts\\python.exe -m app" in message
    assert "Activate.ps1" in message


def test_missing_first_party_module_still_raises():
    with pytest.raises(ModuleNotFoundError):
        run(
            [],
            load_main=_raise_missing("app.missing"),
            stderr=io.StringIO(),
        )


def test_arguments_are_forwarded_to_main():
    seen: list[object] = []

    def _entry(argv):
        seen.append(argv)
        return 0

    assert run(["--list-instances", "--log-level", "DEBUG"], load_main=lambda: _entry) == 0
    assert seen == [["--list-instances", "--log-level", "DEBUG"]]
