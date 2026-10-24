"""子进程工具：统一的 Popen / run 附加参数。

Windows 上从 GUI（pythonw 或 PyInstaller ``--windowed``）拉起 ``java``、Forge
安装器这类控制台程序时，如果不显式指定 ``CREATE_NO_WINDOW`` + ``STARTF_USESHOWWINDOW``，
系统会给子进程分配一个控制台窗口：用户会看到黑框一闪而过，安装器运行期间还会一直留着。

调用方无脑 ``**no_window_kwargs()`` 即可，非 Windows 平台返回空 dict。
"""

from __future__ import annotations

import subprocess
import sys

__all__ = ["no_window_kwargs", "hide_standalone_console"]


def no_window_kwargs() -> dict:
    """返回隐藏子进程控制台窗口的 Popen / run 关键字参数。

    * Windows：``creationflags=CREATE_NO_WINDOW`` + 带 ``STARTF_USESHOWWINDOW`` 的 STARTUPINFO；
    * 其它平台：空 dict（``subprocess`` 不认识这些参数）。
    """
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        "startupinfo": startupinfo,
    }


def hide_standalone_console() -> bool:
    """双击 .py 启动时隐藏控制台黑框；从终端启动时保持可见。

    判断依据是 Windows 的 ``GetConsoleProcessList``：它返回附加到当前控制台的
    进程数。双击 .py 时 explorer 没有控制台，系统会为 python.exe 新分配一个
    **独占**控制台（进程数 = 1），这时隐藏窗口不会让使用者丢掉命令行输出；
    从 cmd / PowerShell 启动时控制台里还有 shell（进程数 >= 2），保持原样，
    方便看 ``--help`` 和报错。

    返回是否真的执行了隐藏；非 Windows、没有控制台、调用失败都返回 False。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        hwnd = kernel32.GetConsoleWindow()
        if not hwnd:
            return False
        buffer = (ctypes.c_uint * 8)()
        attached = int(kernel32.GetConsoleProcessList(buffer, 8))
        if attached > 1:
            return False
        ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
        return True
    except Exception:  # noqa: BLE001 - 隐藏失败不能影响启动
        return False
