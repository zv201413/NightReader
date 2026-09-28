"""Gtk.Application 生命周期与命令行参数。

用法:
    python3 -m nightread <file.pdf> [other.pdf ...]
    python3 -m nightread                 # 空窗口,用 Ctrl+O 打开
"""

from __future__ import annotations

import os
import sys
from typing import Optional

try:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, Gio, GLib
    _HAVE_GTK = True
except Exception:
    Gtk = Gio = GLib = None
    _HAVE_GTK = False

from .window import MainWindow
from .continuous import tune_allocator

APP_ID = "org.nightread.Nightread"


def launch_reader(path: str) -> None:
    """Open another reader process, keeping MuPDF workers isolated.

    GLib reaps the child automatically; closing this window does not close it.
    Pass an argument vector so spaces and shell characters in paths stay literal.
    """
    GLib.spawn_async(
        [sys.executable, "-m", "nightread", os.path.abspath(path)],
        working_directory=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class NightreadApp(Gtk.Application if _HAVE_GTK else object):
    """应用主体。"""

    def __init__(self, path: Optional[str] = None):
        if not _HAVE_GTK:
            raise RuntimeError("GTK 不可用")
        super().__init__(application_id=APP_ID,
                         flags=(Gio.ApplicationFlags.HANDLES_OPEN |
                                Gio.ApplicationFlags.NON_UNIQUE))
        self._initial_path = path
        self.win: Optional[MainWindow] = None

    def do_activate(self) -> None:
        if self.win is None:
            self.win = MainWindow(app=self, path=self._initial_path)
            self.win.connect("destroy", self._reader_destroyed)
        self.win.show_all()
        self.win.present()

    def do_open(self, files, n_files: int, hint: str) -> None:
        """通过文件管理器"用 nightread 打开"时走这里。"""
        paths = [f.get_path() for f in files[:n_files] if f.get_path()]
        if self.win is None and paths:
            self._initial_path = paths.pop(0)
        self.do_activate()
        for path in paths:
            launch_reader(path)

    def _reader_destroyed(self, _window) -> None:
        self.win = None
        self._initial_path = None


def main(argv: Optional[list[str]] = None) -> int:
    # Memory plan P1: tune glibc malloc before any window or worker thread
    # exists. A silent no-op off glibc or with NIGHTREAD_ALLOC_TUNING=0.
    tune_allocator()
    argv = list(sys.argv if argv is None else argv)
    if not _HAVE_GTK:
        print("错误: 需要 GTK3 + PyGObject。", file=sys.stderr)
        return 1

    if "--settings" in argv[1:]:
        from .settings import SettingsApp
        return SettingsApp().run([argv[0]])

    # 将所有文件交给 HANDLES_OPEN,不能只传程序名、把路径藏在本地实例里。
    paths = []
    for a in argv[1:]:
        if not a.startswith("-"):
            if os.path.exists(a):
                paths.append(os.path.abspath(a))
            else:
                print(f"警告: 文件不存在,已忽略: {a}", file=sys.stderr)

    app = NightreadApp()
    return app.run([argv[0], *paths])


if __name__ == "__main__":
    sys.exit(main())
