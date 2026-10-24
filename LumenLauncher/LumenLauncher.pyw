"""Lumen Launcher 无控制台入口（Windows 双击用）。

双击本文件时 Windows 会用 ``pythonw.exe`` 打开它，所以不会出现命令行黑框；
参数与 ``python main.py`` 完全一致，需要命令行参数时仍用 main.py。

（``.pyw`` 后缀在 Windows 上默认关联 pythonw.exe；其它平台可以
``pythonw LumenLauncher.pyw`` 或直接 ``python main.py``。）
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
