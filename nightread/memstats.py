"""Opt-in memory diagnostics; MuPDF inspection always runs on DocWorker."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import sys
import time


def store_size() -> tuple[int, str]:
    """Return MuPDF's accounted cache bytes using supported bindings only.

    PyMuPDF 1.28's TOOLS.store_size is a stub returning None. Its debug output
    lists each store entry and then duplicates the entries in a hash listing;
    sum only the first list. Do not guess offsets inside private C structs.
    Must be called on the document worker, like store_shrink itself.
    """
    import fitz
    size = fitz.TOOLS.store_size
    size = size() if callable(size) else size
    if isinstance(size, int):
        return size, "tools"
    buf = fitz.mupdf.FzBuffer(1024)
    output = fitz.mupdf.FzOutput(buf)
    try:
        fitz.mupdf.fz_debug_store(output)
    finally:
        output.fz_close_output()
    listing = buf.fz_buffer_extract_copy()
    if b"-- resource store contents --" not in listing:
        raise RuntimeError("Unsupported MuPDF store debug format")
    entries = [line for line in listing.splitlines() if line.startswith(b"STORE\tstore[")]
    sizes = [re.match(rb"STORE\tstore\[[^\]]*\]\[refs=\d+\]\[size=(\d+)\]", line)
             for line in entries]
    if any(match is None for match in sizes):
        raise RuntimeError("Unsupported MuPDF store entry format")
    return sum(int(match[1]) for match in sizes), "debug_store"


def _deep_size(value, seen):
    identity = id(value)
    if identity in seen:
        return 0
    seen.add(identity)
    total = sys.getsizeof(value)
    if isinstance(value, dict):
        total += sum(_deep_size(k, seen) + _deep_size(v, seen) for k, v in value.items())
    elif isinstance(value, (list, tuple)):
        total += sum(_deep_size(v, seen) for v in value)
    elif hasattr(value, "__dict__"):
        total += _deep_size(vars(value), seen)
    return total


def index_stats(index):
    return {"pages": len(index) if index is not None else 0,
            "geometry_pages": len(index.pages) if index is not None else 0,
            "chars": index.total_chars if index is not None else 0,
            "estimated_bytes": _deep_size(index, set()) if index is not None else 0}


def process_stats():
    result = {}
    for path, fields in (("/proc/self/status", {"VmRSS", "VmSwap", "VmHWM"}),
                         ("/proc/self/smaps_rollup", {"Pss", "SwapPss"})):
        try:
            for line in Path(path).read_text().splitlines():
                key, _, value = line.partition(":")
                if key in fields:
                    result[key + "_bytes"] = int(value.split()[0]) * 1024
        except (OSError, ValueError):
            pass
    if "VmRSS_bytes" in result and "VmSwap_bytes" in result:
        result["rss_swap_bytes"] = result["VmRSS_bytes"] + result["VmSwap_bytes"]
    if "Pss_bytes" in result and "SwapPss_bytes" in result:
        result["pss_swap_bytes"] = result["Pss_bytes"] + result["SwapPss_bytes"]
    return result


def allocator_stats():
    """Optional glibc malloc_info summary, without writing a temporary file."""
    try:
        if not (os.confstr("CS_GNU_LIBC_VERSION") or "").startswith("glibc"):
            return None
        import ctypes as c
        import xml.etree.ElementTree as ET
        lib = c.CDLL("libc.so.6")
        lib.open_memstream.argtypes = (c.POINTER(c.c_void_p), c.POINTER(c.c_size_t))
        lib.open_memstream.restype = c.c_void_p
        lib.malloc_info.argtypes = (c.c_int, c.c_void_p)
        lib.malloc_info.restype = c.c_int
        lib.fclose.argtypes = (c.c_void_p,)
        lib.fclose.restype = c.c_int
        lib.free.argtypes = (c.c_void_p,)
        lib.free.restype = None
        buffer, length = c.c_void_p(), c.c_size_t()
        stream = lib.open_memstream(c.byref(buffer), c.byref(length))
        if not stream:
            return None
        try:
            try:
                status = lib.malloc_info(0, stream)
            finally:
                lib.fclose(stream)
            if status:
                return None
            root = ET.fromstring(c.string_at(buffer, length.value))
            def size(tag, kind):
                node = root.find(f"{tag}[@type='{kind}']")
                return int(node.get("size", "0")) if node is not None else None
            return {"arenas": len(root.findall("heap")),
                    "arena_system_bytes": size("system", "current"),
                    "arena_free_bytes": size("total", "rest"),
                    "mmap_bytes": size("total", "mmap")}
        finally:
            lib.free(buffer)
    except Exception:
        return None


class MemoryReporter:
    """Coalesce timer/signal requests; never read worker-owned objects on GTK."""
    def __init__(self, worker, view):
        from gi.repository import GLib
        self.worker, self.view = worker, view
        self._closed = self._pending = False
        self._sources = [GLib.timeout_add_seconds(5, self.request, "timer")]
        if hasattr(GLib, "unix_signal_add") and hasattr(signal, "SIGUSR1"):
            self._sources.append(GLib.unix_signal_add(
                GLib.PRIORITY_DEFAULT, signal.SIGUSR1, self.request, "signal"))

    @classmethod
    def start_if_enabled(cls, worker, view):
        if os.environ.get("NIGHTREAD_MEMSTATS") == "1":
            return cls(worker, view)
        return None

    def request(self, reason="manual"):
        if self._closed:
            return False
        if not self._pending:
            from .docworker import Task, MEMORY_STATS
            self._pending = True
            self.worker.submit(Task(MEMORY_STATS, callback=lambda res: self._report(res, reason)))
        return True

    def _report(self, result, reason):
        self._pending = False
        if self._closed:
            return
        record = {"pid": os.getpid(), "time": time.time(), "reason": reason,
                  "process": process_stats(), "view": self.view.memory_stats(),
                  "worker": result.value if result.ok else {"error": str(result.error)},
                  "allocator": allocator_stats()}
        child = record["worker"].get("process", {})
        record["reader_total"] = {
            key: record["process"].get(key, 0) + child.get(key, 0)
            for key in ("rss_swap_bytes", "pss_swap_bytes")}
        try:
            print("NIGHTREAD_MEMSTATS " + json.dumps(record, separators=(",", ":")),
                  file=sys.stderr, flush=True)
        except OSError:
            pass

    def close(self):
        from gi.repository import GLib
        self._closed = True
        for source in self._sources:
            GLib.source_remove(source)
        self._sources.clear()
