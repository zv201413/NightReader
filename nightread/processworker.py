"""Serial document worker in a private process, outside GTK's Python GIL.

The existing queue and callbacks stay in an I/O thread. Only the child owns a
Document, including unsaved edits. Control messages use an inherited pipe;
pixels cross through one anonymous shared buffer, freed after each transfer.
No server, named shared-memory objects or additional dependencies are needed.
"""
from __future__ import annotations

import mmap
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import traceback
from multiprocessing.connection import Connection

import fitz
import numpy as np

from . import docworker as engine


def _pack(value, fd):
    if isinstance(value, fitz.Pixmap):
        size = value.height * value.stride
        os.ftruncate(fd, size)
        with mmap.mmap(fd, size) as buffer:
            np.copyto(np.frombuffer(buffer, np.uint8),
                      np.frombuffer(value.samples_mv, np.uint8))
        return {"__nightread_pixmap__": True, "width": value.width,
                "height": value.height, "x": value.x, "y": value.y,
                "alpha": value.alpha, "channels": value.colorspace.n,
                "size": size, "xres": value.xres, "yres": value.yres}
    if isinstance(value, dict):
        return {k: _pack(v, fd) for k, v in value.items()}
    return value


def _unpack(value, fd):
    if isinstance(value, dict) and value.get("__nightread_pixmap__"):
        cs = {1: fitz.csGRAY, 3: fitz.csRGB, 4: fitz.csCMYK}[value["channels"]]
        x, y, w, h = (value[k] for k in ("x", "y", "width", "height"))
        pix = fitz.Pixmap(cs, (x, y, x + w, y + h), bool(value["alpha"]))
        with mmap.mmap(fd, value["size"], access=mmap.ACCESS_READ) as buffer:
            np.copyto(np.frombuffer(pix.samples_mv, np.uint8),
                      np.frombuffer(buffer, np.uint8))
        pix.set_dpi(value["xres"], value["yres"])
        os.ftruncate(fd, 0)
        return pix
    if isinstance(value, dict):
        return {k: _unpack(v, fd) for k, v in value.items()}
    return value


class ProcessDocWorker(engine.DocWorker):
    """Same Task/Result API and ordered operations as DocWorker."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._process = None
        self._connection = None
        self._pixels_fd = None
        self._ready = False
        self._broken = False

    @property
    def pid(self):
        return self._process.pid if self._process is not None else None

    def cancel_search(self):
        super().cancel_search()
        if self._ready and self._process.poll() is None:
            try:
                self._process.send_signal(signal.SIGUSR1)
            except ProcessLookupError:
                pass

    def _run(self):
        parent, child = socket.socketpair()
        try:
            self._pixels_fd = os.memfd_create("nightread-pixels", os.MFD_CLOEXEC)
            self._process = subprocess.Popen(
                [sys.executable, "-m", "nightread.processworker",
                 str(child.fileno()), str(self._pixels_fd)],
                pass_fds=(child.fileno(), self._pixels_fd),
                cwd=str(Path(__file__).resolve().parents[1]), stdin=subprocess.DEVNULL)
            child.close()
            self._connection = Connection(parent.detach())
            self._ready = self._connection.recv() == "ready"
        except Exception:
            # Startup failure must produce ordinary Result errors for requests,
            # rather than leave the UI waiting forever or replay file writes.
            self._broken = True
        try:
            super()._run()
        finally:
            self._ready = False
            if self._connection is not None:
                self._connection.close()
            parent.close()
            child.close()
            if self._process is not None:
                self._process.wait()
            if self._pixels_fd is not None:
                os.close(self._pixels_fd)
                self._pixels_fd = None

    def _maintain_store(self, clear=False):
        # The child maintains its own cache after each render and close.
        pass

    def _rpc(self, kind, **kwargs):
        if self._broken or not self._ready:
            raise RuntimeError("文档子进程已退出；请重新打开阅读器。")
        try:
            self._connection.send((kind, kwargs))
            reply = self._connection.recv()
        except (EOFError, OSError) as exc:
            self._broken = True
            raise RuntimeError("文档子进程意外退出；未自动重试保存操作。") from exc
        generation = reply["generation"]
        with self._lock:
            changed = generation != self._generation
            self._generation = generation
        if changed and self._on_gen_change:
            self._post_to_ui(lambda g=generation: self._on_gen_change(g))
        if not reply["ok"]:
            raise RuntimeError(reply["error"])
        return _unpack(reply["value"], self._pixels_fd)


def _remote_operation(kind):
    def operation(self, **kwargs):
        return self._rpc(kind, **kwargs)
    return operation


# Keep the operation methods as dispatch seams (including delayed-operation
# integration tests). They forward everything; no parent-side Document exists.
for _name, _kind in {
    "open": engine.OPEN, "close": engine.CLOSE, "render": engine.RENDER,
    "render_text": engine.RENDER_TEXT, "get_toc": engine.GET_TOC,
    "set_toc_item": engine.SET_TOC_ITEM, "set_toc": engine.SET_TOC,
    "search": engine.SEARCH, "build_index": engine.BUILD_INDEX,
    "add_annot": engine.ADD_ANNOT, "del_annot": engine.DEL_ANNOT,
    "get_annots": engine.GET_ANNOTS, "select_text": engine.SELECT_TEXT,
    "save": engine.SAVE, "shrink": engine.SHRINK, "memory_stats": engine.MEMORY_STATS,
}.items():
    setattr(ProcessDocWorker, "_op_" + _name, _remote_operation(_kind))


def serve(connection_fd, pixels_fd):
    from .continuous import tune_allocator, release_free_memory
    from .memstats import process_stats
    tune_allocator()
    worker = engine.DocWorker()
    signal.signal(signal.SIGUSR1, lambda *_: worker.cancel_search())
    connection = Connection(connection_fd)
    connection.send("ready")
    last_activity, trimmed = time.monotonic(), False
    try:
        while True:
            if not connection.poll(.2):
                if not trimmed and time.monotonic() - last_activity >= 1.5:
                    release_free_memory()
                    trimmed = True
                continue
            kind, kwargs = connection.recv()
            try:
                value = worker._dispatch(engine.Task(kind, kwargs))
                if kind in engine.STORE_TASKS:
                    worker._maintain_store()
                if kind == engine.MEMORY_STATS:
                    value.update(pid=os.getpid(), process=process_stats())
                reply = {"ok": True, "value": _pack(value, pixels_fd)}
            except Exception as exc:
                reply = {"ok": False, "error": str(exc), "tb": traceback.format_exc()}
            reply["generation"] = worker.generation
            connection.send(reply)
            value = reply = None
            last_activity, trimmed = time.monotonic(), False
    except (EOFError, BrokenPipeError, ConnectionResetError):
        pass
    finally:
        worker._op_close()
        connection.close()
        os.close(pixels_fd)


if __name__ == "__main__":
    serve(int(sys.argv[1]), int(sys.argv[2]))
