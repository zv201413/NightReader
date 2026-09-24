"""Command-line entry point; help and version work without a display server."""
import argparse
import os
import sys
from . import __version__


def main():
    parser = argparse.ArgumentParser(
        prog="nightreader", description="NightReader — 离线 PDF 夜读、书签与高亮批注")
    parser.add_argument("files", nargs="*", metavar="PDF", help="要打开的 PDF 文件")
    parser.add_argument("--settings", action="store_true", help="打开设置面板")
    parser.add_argument("--version", action="version", version=f"NightReader {__version__}")
    args = parser.parse_args()
    if args.settings and args.files:
        parser.error("--settings 不能与 PDF 文件一起使用")
    from .app import main as run_app
    return run_app([sys.argv[0], *(["--settings"] if args.settings else
                                 [os.path.abspath(path) for path in args.files])])
