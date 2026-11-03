#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""seedpool_cli.py — 离线种子池管理（切段 / 取段 / 换表）

用法：
    py -3.12 seedpool_cli.py status                  # 查看当前表、段游标、剩余
    py -3.12 seedpool_cli.py next                    # 取一段（=模拟一次会话取种）
    py -3.12 seedpool_cli.py refill --bytes 1000000  # 从 H743 采 100 万字节换表（~100 s）
    py -3.12 seedpool_cli.py refill --from other.bin # 用已有 .bin 换表

说明：
  - 每次 `next` 消耗一段，游标持久化在 seedpool_state.json，重启不重复。
  - 段全部用尽后 `next`/离线桥启动会提示换表（TableExhausted）。
  - 换表推荐流程：段用尽 或 表龄超阈值 -> refill（从 TRNG 采新表）-> 游标归零。
"""
from __future__ import annotations

import argparse
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from slot.seedpool import SeedPool, TableExhausted, table_age_days  # noqa: E402


def _cmd_status(pool: SeedPool, seg: int) -> int:
    try:
        pool.load()                      # 确保 state 与表对齐（同指纹保留游标）
    except Exception as e:
        print("✗ 状态不可用:", e)
        return 1
    st = pool.status()
    print("表        :", st.get("table"))
    print("指纹      :", (st.get("sha256") or "")[:16], "...")
    print("大小      :", st.get("size"), "字节 /", st.get("segments"),
          "段（每段", seg, "字节）")
    print("已用/剩余 :", st.get("used"), "/", st.get("remaining"), "段")
    tpath = os.path.join(pool.base, st.get("table") or "")
    print("表龄      : %.1f 天" % table_age_days(tpath))
    if st.get("exhausted"):
        print("⚠ 段已用尽 —— 请 refill 换表")
    return 0


def _cmd_next(pool: SeedPool) -> int:
    try:
        seed, seg = pool.next_seed()
    except TableExhausted as e:
        print("✗", e)
        print("  请执行： py -3.12 seedpool_cli.py refill --bytes 1000000")
        return 2
    print("取段 #%d（%d 字节），表 %s" % (seg, len(seed), pool.state.get("table")))
    print("（种子不打印，避免泄露；审计已追加到 seedpool_audit.jsonl）")
    return 0


def _cmd_refill(pool: SeedPool, a) -> int:
    try:
        if a.src:
            info = pool.refill_from_file(a.src)
            how = "文件 " + a.src
        else:
            info = pool.refill_from_trng(a.bytes)
            how = "随机表 %d 字节" % a.bytes
    except Exception as e:
        print("✗ refill 失败:", e)
        return 1
    print("✓ 新表:", info["name"], "（来源:", how, "）")
    print("  %d 字节 / %d 段，指纹 %s...  游标已归零"
          % (info["size"], info["segments"], info["sha256"][:16]))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="离线种子池管理")
    ap.add_argument("--dir", default=BASE, help="种子池目录（默认脚本目录）")
    ap.add_argument("--seg-bytes", type=int, default=256, help="每段字节数（默认 256）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="查看当前表与段游标")
    sub.add_parser("next", help="取一段（模拟一次会话取种）")
    r = sub.add_parser("refill", help="换表（从 H743 硬件 RNG 或已有文件）")
    r.add_argument("--bytes", type=int, default=1_000_000,
                   help="从 TRNG 采集的字节数（默认 100 万）")
    r.add_argument("--from", dest="src", default=None, help="用已有 .bin 换表")
    a = ap.parse_args()

    pool = SeedPool(a.dir, a.seg_bytes)
    if a.cmd == "status":
        return _cmd_status(pool, a.seg_bytes)
    if a.cmd == "next":
        return _cmd_next(pool)
    if a.cmd == "refill":
        return _cmd_refill(pool, a)
    return 1


if __name__ == "__main__":
    sys.exit(main())
