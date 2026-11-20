#!/usr/bin/env python3
"""一键自检：UI 引用、编译、单元/端到端测试。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(label: str, cmd: list[str]) -> int:
    print(f"\n== {label} ==")
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print(f"失败: {label}")
    return result.returncode


def main() -> int:
    steps = [
        ("编译检查", [sys.executable, "-m", "compileall", "-q", "printroom", "tests", "scripts"]),
        ("页面引用检查", [sys.executable, "scripts/check_ui.py"]),
        ("测试", [sys.executable, "-m", "unittest", "tests.test_printroom"]),
    ]
    code = 0
    for label, cmd in steps:
        code |= run(label, cmd)
    print("\n全部通过" if code == 0 else "\n存在失败项")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
