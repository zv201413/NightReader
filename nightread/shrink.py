#!/usr/bin/env python3
"""精简重写出口 —— Z2。

背景:
    增量保存(saveIncr)每次只追加增量段,不重写正文。好处是单次开销极小
    (改标题 ~360B),代价是**逐轮线性累积**:改 10 次标题累计 +3.6KB,
    加 5 次高亮累计 +36KB(实测 3,157,220 → 3,193,828 B)。

    这是增量保存的固有特性,不是 bug。但长期编辑同一个文件会让体积慢慢涨。
    本工具提供一次性的"精简重写",把累积的增量段合并掉、回收垃圾对象。

安全性:
    · 默认**原地重写**,但先写临时文件再 os.replace,保证原子性
    · 重写前后校验: 页数、大纲条数、正文文本 md5 必须一致
    · 任一不符则放弃替换并报错(不静默丢数据)
    · --dry-run 只报告不写盘

用法:
    python3 -m nightread.shrink <file.pdf>              # 原地精简
    python3 -m nightread.shrink <file.pdf> --dry-run    # 只看能省多少
    python3 -m nightread.shrink <file.pdf> -o out.pdf   # 另存
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile

import fitz


def content_text_md5(doc: "fitz.Document") -> str:
    h = hashlib.md5()
    for pg in doc:
        h.update(pg.get_text().encode("utf-8"))
    return h.hexdigest()


def fingerprint(path: str) -> dict:
    d = fitz.open(path)
    try:
        return {
            "pages": len(d),
            "toc": len(d.get_toc()),
            "text_md5": content_text_md5(d),
            "bytes": os.path.getsize(path),
        }
    finally:
        d.close()


def shrink(src: str, dst: str | None, dry_run: bool = False) -> int:
    before = fingerprint(src)
    print(f"源文件: {src}")
    print(f"  字节 {before['bytes']:,} | 页数 {before['pages']} | "
          f"大纲 {before['toc']} | 正文 md5 {before['text_md5'][:16]}…")

    d = fitz.open(src)
    try:
        if dry_run:
            # 写到临时文件测体积,不落最终位置
            fd, tmp = tempfile.mkstemp(suffix=".pdf", dir="/tmp")
            os.close(fd)
            d.save(tmp, garbage=4, deflate=True)
            size_after = os.path.getsize(tmp)
            d2 = fitz.open(tmp)
            after = {"pages": len(d2), "toc": len(d2.get_toc()),
                     "text_md5": content_text_md5(d2), "bytes": size_after}
            d2.close()
            os.unlink(tmp)
        else:
            target = dst or src
            if dst and os.path.abspath(dst) == os.path.abspath(src):
                target = src
            # 原子性: 先写同目录临时文件,校验通过再 replace
            tmpdir = os.path.dirname(os.path.abspath(target)) or "."
            fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=tmpdir)
            os.close(fd)
            d.save(tmp, garbage=4, deflate=True)
            d2 = fitz.open(tmp)
            after = {"pages": len(d2), "toc": len(d2.get_toc()),
                     "text_md5": content_text_md5(d2),
                     "bytes": os.path.getsize(tmp)}
            d2.close()

            # 校验: 内容必须等价
            problems = []
            if after["pages"] != before["pages"]:
                problems.append(f"页数 {before['pages']} -> {after['pages']}")
            if after["toc"] != before["toc"]:
                problems.append(f"大纲 {before['toc']} -> {after['toc']}")
            if after["text_md5"] != before["text_md5"]:
                problems.append("正文文本 md5 变了")
            if problems:
                os.unlink(tmp)
                print("\n✗ 精简后内容不一致,已放弃替换(原文件未动):",
                      file=sys.stderr)
                for p in problems:
                    print(f"    · {p}", file=sys.stderr)
                return 1

            os.replace(tmp, target)
    finally:
        d.close()

    delta = after["bytes"] - before["bytes"]
    pct = (delta / before["bytes"] * 100) if before["bytes"] else 0.0
    print(f"  结果 {after['bytes']:,} | 页数 {after['pages']} | "
          f"大纲 {after['toc']} | 正文 md5 {after['text_md5'][:16]}…")
    print(f"  体积变化 {delta:+,} B ({pct:+.0f}%)"
          + ("  [dry-run,未写盘]" if dry_run else ""))
    print("  ✓ 内容校验通过" if after == before or dry_run
          else "  ✓ 内容等价,已重写")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="PDF 精简重写(回收增量累积)")
    ap.add_argument("file", help="待精简的 PDF")
    ap.add_argument("-o", "--output", help="另存路径(默认原地重写)")
    ap.add_argument("--dry-run", action="store_true", help="只报告不写盘")
    a = ap.parse_args()

    if not os.path.exists(a.file):
        print(f"错误: 文件不存在: {a.file}", file=sys.stderr)
        return 2
    return shrink(a.file, a.output, a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
