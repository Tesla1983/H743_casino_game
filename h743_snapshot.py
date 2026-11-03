#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h743_snapshot.py —— 采 / 校验 STM32H743 熵快照（离线随机源的种子）。

为什么要有快照：
    串口是独占资源。在线桥（--entropy auto，从 H743 实时取熵）会长期持有 串口端口，
    离线桥如果也去连板子播种，必然 PermissionError。把板子的熵在空闲时采成一份
    快照文件，离线桥只读文件 → 两个随机源可以同时存在，互不抢串口。

安全边界（务必知道）：
    快照 + 派生计数器 = 该离线链可被完整复现。它是"离线可审计"的代价，
    适合演示/复算，不适合当作真实抽奖的唯一熵源。

用法：
    py -3.12 h743_snapshot.py                       # 默认采 4096 B → h743_entropy.json
    py -3.12 h743_snapshot.py --bytes 8192
    py -3.12 h743_snapshot.py --out d:/tmp/h743_entropy.json
    py -3.12 h743_snapshot.py --verify h743_entropy.json
    py -3.12 h743_snapshot.py --verify-only h743_entropy.json   # 同上，仅语义更明确

注意：采快照需要板子空闲（在线桥没在跑）。桥在跑时先停桥，否则会报"被其他程序占用"。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from slot.entropy import (EntropyUnavailable, capture_snapshot,  # noqa: E402
                          load_snapshot, REQ_CAP)


def do_verify(path: str) -> int:
    try:
        raw, doc = load_snapshot(path)
    except Exception as e:
        print(f"[FAIL] {path}: {e}")
        return 1
    size = os.path.getsize(path)
    age_h = (time.time() - float(doc["created"])) / 3600.0
    print(f"[OK] {path}")
    print(f"     熵字节   {len(raw)} B（文件 {size} B，hex 编码）")
    print(f"     采集时间 {doc['created_iso']}（{age_h:.1f} 小时前）")
    print(f"     采集端口 {doc['port']}")
    print(f"     sha256   {doc['sha256']}")
    if age_h > 24 * 30:
        print("     ⚠ 快照超过 30 天，建议重采（旧快照的物理熵统计特性无从复核）")
    return 0


def issue_one(out_path: str, nbytes: int, port, label: str, ledger: str) -> dict:
    """采一份 + 记一行台账。返回快照元数据（含 sha256，供对外公布）。"""
    doc = capture_snapshot(out_path, n=nbytes, port=port)
    rec = {"label": label or "(未标注)", "out": os.path.abspath(out_path),
           "sha256": doc["sha256"], "bytes": doc["bytes"], "port": doc["port"],
           "created_iso": doc["created_iso"]}
    os.makedirs(os.path.dirname(os.path.abspath(ledger)), exist_ok=True)
    with open(ledger, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + chr(10))
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description="STM32H743 熵快照（离线随机源种子）")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "h743_entropy.json"))
    ap.add_argument("--bytes", type=int, default=4096, help=f"采集字节数（1..{REQ_CAP}）")
    ap.add_argument("--port", default=None, help="指定串口（默认按 CH340 + $?PING 认板）")
    ap.add_argument("--label", default="", help="发给谁 / 哪个订单号（写进台账，便于追溯）")
    ap.add_argument("--ledger", default=None,
                    help="台账文件（默认与 --out 同目录的 ledger.jsonl，追加一行）")
    ap.add_argument("--batch", type=int, default=1,
                    help="一次发多份（每份熵独立、各记一行台账）；需串口空闲")
    ap.add_argument("--out-dir", default="snapshots", help="--batch>1 时的输出目录")
    ap.add_argument("--prefix", default="snap", help="--batch>1 时的文件名/标签前缀")
    ap.add_argument("--verify", metavar="PATH", help="只校验已有快照（不碰串口）")
    ap.add_argument("--verify-only", metavar="PATH", help="同 --verify")
    a = ap.parse_args()

    target = a.verify or a.verify_only
    if target:
        return do_verify(target)

    # 台账默认落在"输出旁边"：批量时跟 --out-dir 走，单份时跟 --out 走
    if a.ledger:
        ledger = a.ledger
    elif a.batch and a.batch > 1:
        ledger = os.path.join(a.out_dir, "ledger.jsonl")
    else:
        ledger = os.path.join(os.path.dirname(os.path.abspath(a.out)), "ledger.jsonl")

    # 批量发号：一次采 N 份，每份都是 H743 的新鲜物理熵（互不相同），各记台账
    if a.batch and a.batch > 1:
        os.makedirs(a.out_dir, exist_ok=True)
        print(f"[batch] 发 {a.batch} 份，目录 {os.path.abspath(a.out_dir)}（串口需空闲）")
        rows = []
        try:
            for i in range(1, a.batch + 1):
                name = f"{a.prefix}_{i:03d}.json"
                label = f"{a.prefix}_{i:03d}"
                rec = issue_one(os.path.join(a.out_dir, name), a.bytes, a.port, label, ledger)
                rows.append(rec)
                print(f"  [{i:3d}/{a.batch}] {name}  {rec['sha256'][:16]}…")
        except EntropyUnavailable as e:
            print(f"[FAIL] 第 {len(rows)+1} 份采集失败：{e}")
            print("  排查：① 板子/CH340；② 在线桥占着串口（先停桥）；③ --port COMx 指定。")
            print(f"  已完成 {len(rows)} 份（台账已写入，可继续用）")
            return 1
        except Exception as e:
            print(f"[FAIL] {type(e).__name__}: {e}（已完成 {len(rows)} 份）")
            return 1
        print(f"[OK] 共 {len(rows)} 份，台账 {os.path.abspath(ledger)}")
        print("  建议：每份只发一个人，并把对应的 sha256 先公布（承诺），事后连文件一起给")
        return 0

    try:
        rec = issue_one(a.out, a.bytes, a.port, a.label, ledger)
    except EntropyUnavailable as e:
        print(f"[FAIL] {e}")
        print("  排查：① 板子/CH340 是否插好；② 在线桥是否占着串口（先停桥）；")
        print("        ③ 用 --port COMx 指定（默认自动认板）。")
        return 1
    except Exception as e:
        print(f"[FAIL] 采集异常：{type(e).__name__}: {e}")
        return 1

    print(f"[台账] {ledger}  ← {rec['label']}")
    print(f"[OK] 已写入 {a.out}")
    print(f"     {rec['bytes']} B / 端口 {rec['port']} / sha256 {rec['sha256']}")
    print("  可先公布这个哈希（承诺），事后连快照一起给他即可验证")
    print("  离线桥用法：py -3.12 trng_server.py --offline --port 8770 --entropy file")
    return 0


if __name__ == "__main__":
    sys.exit(main())
