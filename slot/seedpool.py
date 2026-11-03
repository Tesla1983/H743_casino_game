"""离线种子池：把 .bin 随机表切成定长段，每次会话取一段喂 DRBG。

设计要点（与硬件解耦，但仍要"每次会话不同序列"）：
  - 一份 .bin 表 -> 按 SEG_BYTES 切段；每段 = 一次"会话种子"（>=44 字节，DRBG 用前 44）。
  - 段游标持久化到 `seedpool_state.json`（含表名 + sha256 指纹），重启后**不重复用同一段**。
  - 每次取段 append 一条审计记录到 `seedpool_audit.jsonl`（时间 / 段号 / 表名 / 指纹）。
  - 段全部用尽 -> 抛 `TableExhausted`，由调用方触发"换表"（refill）。

红线：
  - **绝不循环重用**同一段（用完即弃，不 wrap-around）；
  - 固定 seed = 固定序列（可复现但也**可预测**）—— 所以每次会话必须换段/换表，
    且表本身应保密；仅当用途是"离线演示 / 可审计复算"时才接受可预测性。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime

SEG_BYTES = 256                 # 每段字节数（>=44；多取的熵可留作 future personalization）
DEFAULT_TABLE = "random_raw.bin"
STATE_NAME = "seedpool_state.json"
AUDIT_NAME = "seedpool_audit.jsonl"


class TableExhausted(Exception):
    """当前表的所有段都已消费完。调用方应换表（refill）。"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


class SeedPool:
    def __init__(self, base_dir: str, seg_bytes: int = SEG_BYTES):
        self.base = os.path.abspath(base_dir)
        self.seg_bytes = int(seg_bytes)
        self.state_path = os.path.join(self.base, STATE_NAME)
        self.audit_path = os.path.join(self.base, AUDIT_NAME)
        self.state: dict = self._load_state()

    # ---------------- 内部 ----------------
    def _load_state(self) -> dict:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path)          # 原子替换，避免半截状态

    def _table_path(self, name: str) -> str:
        return name if os.path.isabs(name) else os.path.join(self.base, name)

    def _table_info(self, name: str) -> dict:
        p = self._table_path(name)
        if not os.path.exists(p):
            raise FileNotFoundError(f"随机表不存在: {p}")
        size = os.path.getsize(p)
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return {"name": os.path.basename(name), "path": p, "size": size,
                "sha256": h.hexdigest(), "segments": size // self.seg_bytes}

    def _audit(self, rec: dict) -> None:
        with open(self.audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ---------------- 公开 API ----------------
    def load(self, name: str | None = None) -> dict:
        """加载/切换随机表。同一张表（指纹一致）保留游标，否则游标归零。"""
        if name is None:
            name = self.state.get("table") or DEFAULT_TABLE
        info = self._table_info(name)
        same = (self.state.get("table") == info["name"]
                and self.state.get("sha256") == info["sha256"]
                and int(self.state.get("segments", -1)) == info["segments"])
        if same:
            self.state.setdefault("next_segment", 0)
        else:
            self.state = {
                "table": info["name"], "sha256": info["sha256"],
                "size": info["size"], "segments": info["segments"],
                "next_segment": 0, "created": _now(),
            }
        self._save_state()
        return info

    def next_seed(self) -> tuple[bytes, int]:
        """取下一段种子，返回 (seed_bytes, segment_index)。段用尽抛 TableExhausted。"""
        if not self.state:
            self.load()
        nxt = int(self.state.get("next_segment", 0))
        segs = int(self.state.get("segments", 0))
        if nxt >= segs:
            raise TableExhausted(
                f"表 {self.state.get('table')} 的 {segs} 段已用尽，请 refill 换表")
        info = self._table_info(self.state["table"])
        if info["sha256"] != self.state.get("sha256"):
            raise RuntimeError("随机表指纹变化（被替换/篡改），请重新 load 或 refill")
        with open(info["path"], "rb") as f:
            f.seek(nxt * self.seg_bytes)
            seed = f.read(self.seg_bytes)
        if len(seed) < 44:
            raise RuntimeError("末段过短（<44 字节），请换表")
        self.state["next_segment"] = nxt + 1
        self._save_state()
        self._audit({"t": _now(), "seg": nxt, "table": self.state["table"],
                     "sha256": self.state["sha256"], "seg_bytes": self.seg_bytes})
        return seed, nxt

    def status(self) -> dict:
        st = dict(self.state)
        segs = int(st.get("segments", 0))
        nxt = int(st.get("next_segment", 0))
        st["used"] = nxt
        st["remaining"] = max(0, segs - nxt)
        st["exhausted"] = (segs > 0 and nxt >= segs)
        return st

    # ---------------- 换表 ----------------
    def refill_from_file(self, src_path: str) -> dict:
        """用已有 .bin 换表（复制成带时间戳的新表，避免覆盖来源）。"""
        if not os.path.exists(src_path):
            raise FileNotFoundError(src_path)
        dst = os.path.join(self.base, f"seedpool_{_stamp()}.bin")
        with open(src_path, "rb") as fi, open(dst, "wb") as fo:
            for chunk in iter(lambda: fi.read(1 << 20), b""):
                fo.write(chunk)
        return self.load(dst)

    def refill_from_trng(self, n: int = 1_000_000, port_timeout: float = 2.0) -> dict:
        """从 H743 的硬件 RNG 采集 n 字节做新表（需要板子空闲；约 10 KB/s，1 MB 要 ~100 s）。"""
        from .entropy import H743Link                # 延迟 import：只读文件时不需要 pyserial
        n = max(int(n), self.seg_bytes * 2)
        link = H743Link(log=print)
        if not link.open():
            raise RuntimeError(f"H743 打不开：{link.last_error or '未应答'}")
        data = bytearray()
        try:
            while len(data) < n:
                data += link.read(min(4096, n - len(data)))
        finally:
            link.close()
        dst = os.path.join(self.base, f"seedpool_{_stamp()}.bin")
        with open(dst, "wb") as f:
            f.write(data)
        return self.load(dst)


def table_age_days(table_path: str) -> float:
    try:
        return (time.time() - os.path.getmtime(table_path)) / 86400.0
    except OSError:
        return 0.0
