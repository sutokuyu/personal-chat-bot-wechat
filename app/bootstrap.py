"""CLI bootstrap that turns a missing dependency into an actionable message."""

from __future__ import annotations

import os
import sys
from typing import Callable, Sequence, TextIO

_HINT = """\
无法启动：当前 Python 缺少依赖 {missing}。

原因通常是用了系统 Python，而不是项目的 .venv。
当前解释器：{executable}

请改用虚拟环境里的解释器：
  cd {cwd}
  .\\.venv\\Scripts\\python.exe -m app --list-memories

或者在 PowerShell 里先激活虚拟环境：
  .\\.venv\\Scripts\\Activate.ps1

如果 .venv 还没建好：
  py -3.12 -m venv .venv
  .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt
"""


def _load_main() -> Callable[[Sequence[str] | None], int]:
    from app.main import main

    return main


def run(
    argv: Sequence[str] | None = None,
    *,
    load_main: Callable[[], Callable[[Sequence[str] | None], int]] | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the CLI, explaining missing third-party packages instead of crashing."""

    loader = load_main or _load_main
    try:
        entry = loader()
    except ModuleNotFoundError as exc:
        missing = exc.name or "(未知模块)"
        # A missing first-party module means a broken checkout, not a bad venv.
        if missing.split(".")[0] == "app":
            raise
        (stderr or sys.stderr).write(
            _HINT.format(
                missing=missing,
                executable=sys.executable,
                cwd=os.getcwd(),
            )
        )
        return 2
    return entry(argv)


__all__ = ["run"]
