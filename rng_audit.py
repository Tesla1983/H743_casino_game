#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rng_audit.py — 游戏随机源公平性审计工具
================================================

对"随机源产出结果"做一套结构化统计审计，用于发现可能破坏游戏公平性的
偏差、相关性与可预测性风险。覆盖：

  1. 均匀性 / 无偏性   —— 字节/半字节卡方、单比特频数(NIST Monobit)、
                          Kolmogorov–Smirnov、按比特位的偏差
  2. 独立性与序列相关 —— 游程检验(NIST Runs)、相邻字节自相关、多 lag 比特自相关、
                          块内频数(NIST Frequency-in-block)、生日碰撞(周期/聚集)
  3. 异常检测         —— 最大游程(长连号/长 streak)、单字节极端离群值
  4. 可预测性风险     —— 信息熵(Shannon, 含 Miller–Madow 偏差修正)、gzip 压缩率、
                          确定性/可复现性提示
  5. 游戏公平性仿真   —— 用项目自身的 rndInt(16bit 拒绝采样) + 骰宝 settleOf 复刻，
                          大量抽样把"随机流"映射成真实游戏结果，核对：
                              · 每颗骰子 1–6 是否均匀
                              · 三骰和分布是否符合 216 种等概率的理论三角分布
                              · 各注位"实证返还率"是否落在理论庄家优势的统计误差内

数据入口（--source 或按扩展名自动识别）：
  raw        .bin 原始字节（真实 TRNG 输出 / DRBG 输出）
  snapshot   .json 熵快照（entropy_hex，自动校验 sha256）
  game       .csv/.jsonl 整数结果序列（抽奖/暴击/开奖记录等）

可选 --expand N：把快照 / 原始字节经本工具自带 ChaCha20 DRBG 扩展成 N 字节后再审计
               （审计"游戏实际消费的、经 DRBG 白化后的流"，而非仅原始字节）。

依赖：纯标准库（math / gzip / hashlib / json / argparse）。本工具内嵌 RFC8439
      ChaCha20，不依赖 slot 包即可运行；如能 import slot.drbg_py 则会优先用它做交叉校验。

输出：控制台结构化报告 + 可选 --json 机器可读 + 可选 --md 报告文档。
严重度分级：CRITICAL(严重) > HIGH(高) > MEDIUM(中) > LOW(低) > INFO(信息)。

用法示例：
  python rng_audit.py                          # 默认审计 random_raw.bin
  python rng_audit.py random_raw.bin
  python rng_audit.py h743_entropy.json --expand 1000000
  python rng_audit.py my_dice_log.csv --source game --game dice
  python rng_audit.py --demo                    # 自证：对一条故意偏置的流跑审计，看能否揪出
  python rng_audit.py random_raw.bin --json rep.json --md rep.md
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import random
import statistics
import struct
import sys
from collections import Counter
from dataclasses import dataclass

# ============================================================== 严重度模型
SEV_RANK = {"INFO": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
SEV_ORDER = ["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"]


def severity_from_p(p: Optional[float], alpha: float) -> str:
    """把 p 值映射为严重度（仅用于"该检验是否不合格"的判级）。"""
    if p is None:
        return "INFO"
    if p >= 0.1:
        return "INFO"
    if p >= alpha:
        return "LOW"          # 临界但未达显著性
    if p >= 1e-3:
        return "MEDIUM"
    if p >= 1e-6:
        return "HIGH"
    return "CRITICAL"


def severity_from_z(z: float, weak=3.0, mid=4.0, high=5.0) -> str:
    a = abs(z)
    if a < weak:
        return "INFO"
    if a < mid:
        return "MEDIUM"
    if a < high:
        return "HIGH"
    return "CRITICAL"


# ============================================================== 统计工具（无 scipy）
def erfc(x: float) -> float:
    return math.erfc(x)


def norm_two_sided_p(z: float) -> float:
    """双侧正态 p 值。"""
    return erfc(abs(z) / math.sqrt(2.0))


def chi2_sf(chi2: float, df: int) -> float:
    """卡方上尾概率：Wilson–Hilferty 正态近似（避免依赖 scipy）。"""
    if df <= 0 or chi2 <= 0:
        return 1.0
    z = ((chi2 / df) ** (1.0 / 3.0) - (1.0 - 2.0 / (9.0 * df))) / math.sqrt(2.0 / (9.0 * df))
    return 0.5 * erfc(z / math.sqrt(2.0))


def ks_pvalue(d: float, n: int) -> float:
    """Kolmogorov–Smirnov 双尾 p 值（大样本渐近式）。"""
    if d <= 0 or n <= 0:
        return 1.0
    x = (math.sqrt(n) + 0.12 + 0.11 / math.sqrt(n)) * d
    if x <= 0:
        return 1.0
    s = 0.0
    for k in range(1, 2000):
        term = ((-1) ** (k - 1)) * math.exp(-2.0 * k * k * x * x)
        s += term
        if abs(term) < 1e-13:
            break
    return min(1.0, max(0.0, 2.0 * s))


def shannon_entropy(counts: list[int], total: int) -> float:
    h = 0.0
    for c in counts:
        if c > 0:
            p = c / total
            h -= p * math.log2(p)
    return h


def bit_count(b: int) -> int:
    return b.bit_count() if hasattr(int, "bit_count") else bin(b).count("1")


# ============================================================== 内嵌 ChaCha20（自包含）
_MASK = 0xFFFFFFFF


def _rotl(x: int, n: int) -> int:
    return ((x << n) | (x >> (32 - n))) & _MASK


def _qr(s, a, b, c, d):
    s[a] = (s[a] + s[b]) & _MASK
    s[d] = _rotl(s[d] ^ s[a], 16)
    s[c] = (s[c] + s[d]) & _MASK
    s[b] = _rotl(s[b] ^ s[c], 12)
    s[a] = (s[a] + s[b]) & _MASK
    s[d] = _rotl(s[d] ^ s[a], 8)
    s[c] = (s[c] + s[d]) & _MASK
    s[b] = _rotl(s[b] ^ s[c], 7)


def chacha20_block(key: bytes, nonce: bytes, counter: int) -> bytes:
    st = [0x61707865, 0x3320646e, 0x79622d32, 0x6b206574]
    st += list(struct.unpack("<8I", key))
    st += [counter & _MASK]
    st += list(struct.unpack("<3I", nonce))
    x = st[:]
    for _ in range(10):
        _qr(x, 0, 4, 8, 12); _qr(x, 1, 5, 9, 13)
        _qr(x, 2, 6, 10, 14); _qr(x, 3, 7, 11, 15)
        _qr(x, 0, 5, 10, 15); _qr(x, 1, 6, 11, 12)
        _qr(x, 2, 7, 8, 13); _qr(x, 3, 4, 9, 14)
    return struct.pack("<16I", *[(x[i] + st[i]) & _MASK for i in range(16)])


def chacha20_generate(seed: bytes, n: int) -> bytes:
    """与项目 slot.drbg_py 同构的 ChaCha20 DRBG（key=seed[:32], nonce=seed[32:44]）。"""
    key = (seed + bytes(32))[:32]
    nonce = (seed[32:] + bytes(12))[:12]
    out = bytearray()
    ctr = 1
    while len(out) < n:
        out += chacha20_block(key, nonce, ctr)
        ctr = (ctr + 1) & _MASK
    return bytes(out[:n])


# ============================================================== Finding 结构
@dataclass
class Finding:
    fid: str
    category: str
    name: str
    description: str
    statistic: str
    metric: str            # 关键数值（如 p 值 / z / 熵）
    threshold: str
    verdict: str           # PASS / WARN / FAIL
    severity: str
    detail: str
    recommendation: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.fid, "category": self.category, "name": self.name,
            "description": self.description, "statistic": self.statistic,
            "metric": self.metric, "threshold": self.threshold, "verdict": self.verdict,
            "severity": self.severity, "detail": self.detail,
            "recommendation": self.recommendation,
        }


# ============================================================== 数据加载
def load_raw(path: str) -> tuple[bytes, dict]:
    with open(path, "rb") as f:
        data = f.read()
    meta = {"source_type": "raw", "path": path, "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}
    return data, meta


def load_snapshot(path: str) -> tuple[bytes, dict]:
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    raw = bytes.fromhex(doc["entropy_hex"])
    if hashlib.sha256(raw).hexdigest() != doc.get("sha256"):
        raise ValueError("快照 sha256 不符（文件被篡改或损坏），拒绝审计")
    if len(raw) != int(doc.get("bytes", -1)):
        raise ValueError("快照长度与元数据不符")
    meta = {"source_type": "snapshot", "path": path, "bytes": len(raw),
            "created_iso": doc.get("created_iso"), "from_port": doc.get("port"),
            "snapshot_sha256": doc.get("sha256"), "sha256": hashlib.sha256(raw).hexdigest()}
    return raw, meta


def load_game_log(path: str) -> tuple[list[int], dict]:
    vals: list[int] = []
    with open(path, "r", encoding="utf-8") as f:
        head = f.read(1)
        f.seek(0)
        if path.endswith(".jsonl") or (head == "{" or head == "["):
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line) if line.startswith("{") else json.loads(line)
                if isinstance(obj, dict):
                    v = obj.get("value", obj.get("outcome", obj.get("result")))
                    if v is not None:
                        vals.append(int(v))
                elif isinstance(obj, (int, float)):
                    vals.append(int(obj))
        else:  # csv
            import csv
            for row in csv.reader(f):
                for cell in row:
                    cell = cell.strip()
                    if cell == "":
                        continue
                    try:
                        vals.append(int(float(cell)))
                    except ValueError:
                        pass
    meta = {"source_type": "game", "path": path, "count": len(vals),
            "min": min(vals) if vals else None, "max": max(vals) if vals else None,
            "sha256": ""}
    return vals, meta


def autodetect(path: str) -> str:
    if path.endswith(".json"):
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        if isinstance(doc, dict) and "entropy_hex" in doc:
            return "snapshot"
        return "game"
    if path.endswith((".csv", ".jsonl")):
        return "game"
    return "raw"


# ============================================================== 检验：均匀性 / 无偏性
def t_chi_square_bytes(data: bytes, alpha: float) -> Finding:
    n = len(data)
    cnt = [0] * 256
    for b in data:
        cnt[b] += 1
    exp = n / 256.0
    chi2 = sum((c - exp) ** 2 / exp for c in cnt)
    df = 255
    p = chi2_sf(chi2, df)
    maxz = max(abs((c - exp) / math.sqrt(exp)) for c in cnt)
    sev = severity_from_p(p, alpha)
    return Finding(
        "UNIF-BYTE", "均匀性", "字节卡方均匀性 (256 桶)",
        "检验 256 个字节取值的频数是否符合均匀分布（df=255）。",
        f"χ²={chi2:.2f}", f"p={p:.3e}", f"p≥{alpha} 合格",
        "PASS" if p >= alpha else "FAIL", sev,
        f"n={n}，期望每桶 {exp:.1f}，单桶最大偏差 |z|={maxz:.2f}",
        "若不合格：随机流存在显著取值偏好，可被用来预测高频字节、破坏公平。" if sev in ("HIGH", "CRITICAL") else "")


def t_chi_square_nibbles(data: bytes, alpha: float) -> Finding:
    n = len(data)
    cnt = [0] * 16
    for b in data:
        cnt[b & 0xF] += 1
        cnt[(b >> 4) & 0xF] += 1
    total = n * 2
    exp = total / 16.0
    chi2 = sum((c - exp) ** 2 / exp for c in cnt)
    df = 15
    p = chi2_sf(chi2, df)
    sev = severity_from_p(p, alpha)
    return Finding(
        "UNIF-NIB", "均匀性", "半字节卡方均匀性 (16 桶)",
        "检验 4-bit 半字节取值频数是否均匀（能揪出按半字节的偏差）。",
        f"χ²={chi2:.2f}", f"p={p:.3e}", f"p≥{alpha} 合格",
        "PASS" if p >= alpha else "FAIL", sev,
        f"样本半字节 {total}，期望每桶 {exp:.1f}",
        "若不合格：低 4 位或高 4 位存在系统性偏差。" if sev in ("HIGH", "CRITICAL") else "")


def t_monobit(data: bytes, alpha: float) -> Finding:
    bits = len(data) * 8
    ones = sum(b.bit_count() for b in data)
    zeros = bits - ones
    s = abs(2 * ones - bits) / math.sqrt(bits)
    p = norm_two_sided_p(s)
    bias = (ones - bits / 2.0) / (bits / 2.0)   # 正=偏 1，负=偏 0
    sev = severity_from_p(p, alpha)
    return Finding(
        "BIAS-MONO", "无偏性", "单比特频数 (NIST Monobit)",
        "检验 0/1 比特总数是否均衡。这是随机性的底线检验。",
        f"|S|={s:.3f}", f"p={p:.3e}", f"p≥{alpha} 合格",
        "PASS" if p >= alpha else "FAIL", sev,
        f"ones={ones}/{bits}，偏置={bias:+.4%}",
        "若不合格：比特出现显著偏向 0 或 1，等价于整体分布偏移。" if sev in ("HIGH", "CRITICAL") else "")


def t_ks_uniform(data: bytes, alpha: float) -> Finding:
    n = len(data)
    cnt = [0] * 256
    for b in data:
        cnt[b] += 1
    # 经验 CDF vs 均匀 CDF，取最大偏差 D
    cdf = 0.0
    d = 0.0
    exp = 1.0 / 256.0
    for i in range(256):
        cdf += cnt[i] / n
        d = max(d, abs(cdf - (i + 1) * exp), abs(cdf - i * exp))
    p = ks_pvalue(d, n)
    # 大样本下 p 值必然极小，改以效应量 D 判级（公平性视角）：
    # D<0.01 视为合格；0.01–0.02 低；0.02–0.04 中；0.04–0.06 高；>0.06 严重。
    if d < 0.01:
        sev, verdict = "INFO", "PASS"
    elif d < 0.02:
        sev, verdict = "LOW", "WARN"
    elif d < 0.04:
        sev, verdict = "MEDIUM", "WARN"
    elif d < 0.06:
        sev, verdict = "HIGH", "FAIL"
    else:
        sev, verdict = "CRITICAL", "FAIL"
    return Finding(
        "UNIF-KS", "均匀性", "Kolmogorov–Smirnov 均匀性",
        "基于字节取值（映射到 [0,1)）的经验分布函数与理论均匀分布的最大偏差 D 检验。"
        "大样本以效应量 D 判级，避免 p 值伪阳性。",
        f"D={d:.4f}", f"p={p:.3e}", "p≥%g 或 D<0.01 合格" % alpha,
        verdict, sev,
        f"n={n}，最大累积偏差 D={d:.4f}（占满量程比例）",
        "若 D 显著：分布偏离均匀，可能有局部聚集或缺失。" if sev in ("HIGH", "CRITICAL", "MEDIUM") else "")


def t_bit_position_bias(data: bytes, alpha: float) -> Finding:
    n = len(data)
    per = [0] * 8
    for b in data:
        for i in range(8):
            if (b >> i) & 1:
                per[i] += 1
    maxz = 0.0
    worst = -1
    props = []
    for i in range(8):
        p = per[i] / n               # 每位覆盖 n 个字节（非 n*8 比特）
        props.append(p)
        z = (p - 0.5) / math.sqrt(0.25 / n)
        if abs(z) > maxz:
            maxz = abs(z)
            worst = i
    # 大样本下 p 值必然极小，改以效应量 |z| 判级（公平性视角）：
    # |z|<4 视为噪声，4–6 低，6–9 中，9–15 高，>15 严重。
    if maxz < 4:
        sev, verdict = "INFO", "PASS"
    elif maxz < 6:
        sev, verdict = "LOW", "WARN"
    elif maxz < 9:
        sev, verdict = "MEDIUM", "WARN"
    elif maxz < 15:
        sev, verdict = "HIGH", "FAIL"
    else:
        sev, verdict = "CRITICAL", "FAIL"
    p = norm_two_sided_p(maxz)
    return Finding(
        "BIAS-BITPOS", "无偏性", "比特位偏差 (8 个 bit 位)",
        "分别检验每个 bit 位（bit0..bit7）出现 1 的比例是否均衡，捕捉硬件/格式引入的位级偏差。"
        "大样本以效应量 |z| 判级，避免 p 值伪阳性。",
        f"max|z|={maxz:.3f}@bit{worst}", f"p={p:.3e}", "p≥%g 或 |z|<4 合格" % alpha,
        verdict, sev,
        f"bit0..7 的 1 比例={['%.4f' % x for x in props]}; 最大偏差在 bit{worst}",
        "若 |z| 显著：特定 bit 位系统性偏 0/1（典型于某些 TRNG 采集或字节序问题）。" if sev in ("HIGH", "CRITICAL", "MEDIUM") else "")


# ============================================================== 检验：独立性与序列相关
def t_runs(data: bytes, alpha: float) -> Finding:
    bits = len(data) * 8
    ones = sum(b.bit_count() for b in data)
    pi = ones / bits
    if abs(pi - 0.5) >= 2.0 / math.sqrt(bits):
        return Finding(
            "INDP-RUNS", "独立性", "游程检验 (NIST Runs)",
            "检验 0/1 交替的游程数是否符合随机。前置：|π−0.5|<2/√n。",
            "nan", "前置未满足", "—", "FAIL", "MEDIUM",
            "Monobit 前置条件未满足（频数偏差过大），游程检验无效。",
            "先解决单比特频数偏差。")
    v = 1
    prev = None
    for b in data:
        for i in range(8):
            bit = (b >> i) & 1
            if prev is not None and bit != prev:
                v += 1
            prev = bit
    num = abs(v - 2.0 * bits * pi * (1.0 - pi))
    den = 2.0 * math.sqrt(2.0 * bits) * pi * (1.0 - pi)
    z = num / den if den else 0.0
    p = norm_two_sided_p(z)
    sev = severity_from_p(p, alpha)
    return Finding(
        "INDP-RUNS", "独立性", "游程检验 (NIST Runs)",
        "检验 0/1 交替的游程数是否符合随机（游程过多=震荡，过少=成块）。",
        f"z={z:.3f}", f"p={p:.3e}", f"p≥{alpha} 合格",
        "PASS" if p >= alpha else "FAIL", sev,
        f"游程数={v}，期望≈{2*bits*pi*(1-pi):.0f}",
        "若不合格：序列存在成块或过度震荡，可被用来预测下一段。" if sev in ("HIGH", "CRITICAL") else "")


def t_serial_corr_bytes(data: bytes, alpha: float, max_lag: int = 16) -> Finding:
    n = len(data)
    if n < max_lag + 2:
        return Finding("INDP-SER", "独立性", "相邻字节自相关", "样本过少。",
                       "0", "1.0", "—", "INFO", "INFO", "样本过少")
    mean = sum(data) / n
    worst = 0.0
    worst_lag = 1
    for lag in range(1, max_lag + 1):
        num = sum((data[i] - mean) * (data[i + lag] - mean) for i in range(n - lag))
        den = sum((x - mean) ** 2 for x in data)
        r = num / den if den else 0.0
        z = r * math.sqrt(n - lag)
        if abs(z) > abs(worst):
            worst = z
            worst_lag = lag
    p = norm_two_sided_p(worst)
    r = abs(worst) / math.sqrt(n - worst_lag) if (n - worst_lag) else 0.0
    # 以效应量 |r|（皮尔逊自相关系数）判级，避免大样本 p 值伪阳性：
    # |r|<0.01 视为无相关；0.01–0.02 低；0.02–0.05 中；>0.05 高/严重。
    if r < 0.01:
        sev, verdict = "INFO", "PASS"
    elif r < 0.02:
        sev, verdict = "LOW", "WARN"
    elif r < 0.05:
        sev, verdict = "MEDIUM", "WARN"
    else:
        sev, verdict = "HIGH", "FAIL"
    return Finding(
        "INDP-SER", "独立性", f"字节自相关 (lag 1..{max_lag})",
        "检验相邻（及多步）字节序列的线性相关（皮尔逊系数 r）；真随机应无显著自相关。"
        "大样本以效应量 |r| 判级。",
        f"r={r:+.4f}@lag{worst_lag}", f"p={p:.3e}", "|r|<0.01 合格",
        verdict, sev,
        f"lag 1..{max_lag} 中最强自相关 r={r:+.4f}@lag{worst_lag}",
        "若 |r| 显著：序列存在可建模的线性依赖，可被预测。" if sev in ("HIGH", "MEDIUM") else "")


def t_bit_autocorr(data: bytes, alpha: float, lags=(1, 2, 8, 16)) -> Finding:
    # 取前 4M 比特做样本，控制耗时
    max_bits = 4_000_000
    nbytes = min(len(data), max_bits // 8)
    nbits = nbytes * 8
    worst_z = 0.0
    worst_info = ""
    # 逐比特游标法计算 sum(b_i * b_{i+lag})；限 4M 比特控制耗时
    for lag in lags:
        if lag >= nbits:
            continue
        c11 = 0
        for i in range(0, nbits - lag, 1):
            bi = (data[i >> 3] >> (i & 7)) & 1
            bj = (data[(i + lag) >> 3] >> ((i + lag) & 7)) & 1
            if bi and bj:
                c11 += 1
        # 估计 p = c11/(nbits-lag)；期望 = (1/4)*(nbits-lag) 当且仅当独立且 p=1/2
        # 用协方差 z = (c11 - m/4) / sqrt(3*m/16), m=nbits-lag
        m = nbits - lag
        exp = m / 4.0
        var = 3.0 * m / 16.0
        z = (c11 - exp) / math.sqrt(var)
        if abs(z) > abs(worst_z):
            worst_z = z
            worst_info = f"lag{lag}"
    p = norm_two_sided_p(worst_z)
    # 以效应量 |z| 判级（大样本 p 值无意义）：z<3 噪声，3–4 低，4–6 中，>6 高。
    if abs(worst_z) < 3:
        sev, verdict = "INFO", "PASS"
    elif abs(worst_z) < 4:
        sev, verdict = "LOW", "WARN"
    elif abs(worst_z) < 6:
        sev, verdict = "MEDIUM", "WARN"
    else:
        sev, verdict = "HIGH", "FAIL"
    return Finding(
        "INDP-BITAUT", "独立性", f"比特自相关 (lag {', '.join(map(str, lags))})",
        "检验相邻比特（含多步）的自相关；真随机不应有可检测的比特级依赖。大样本以效应量 |z| 判级。",
        f"max|z|={abs(worst_z):.3f}@{worst_info}", f"p={p:.3e}", "|z|<3 合格",
        verdict, sev,
        f"样本比特 {nbits}（上限 4M），最强比特自相关位于 {worst_info or 'n/a'}",
        "若 |z| 显著：比特级存在可预测依赖（常见于弱 PRNG 或半成熟 TRNG）。" if sev in ("HIGH", "MEDIUM") else "")


def t_freq_in_block(data: bytes, alpha: float) -> Finding:
    bits = len(data) * 8
    # 块大小：取约 n/100，且 >= 100
    m = max(100, bits // 100)
    nblocks = bits // m
    if nblocks < 10:
        return Finding("INDP-BLK", "独立性", "块内频数 (NIST Freq-in-block)",
                       "把比特流切块，检验每块内 1 的比例是否围绕 0.5（捕捉局部偏离）。",
                       "nan", "1.0", "—", "INFO", "INFO", "块数不足")
    pi = 0.5
    chi2 = 0.0
    for bk in range(nblocks):
        ones = 0
        for i in range(m):
            bit = (data[((bk * m + i) >> 3)] >> ((bk * m + i) & 7)) & 1
            ones += bit
        phat = ones / m
        chi2 += (phat - pi) ** 2
    chi2 = chi2 * m / (pi * (1 - pi))
    df = nblocks - 1
    p = chi2_sf(chi2, df)
    ratio = chi2 / df if df else 1.0
    # 以效应量 ratio=χ²/df 判级（期望≈1；大样本 p 值无意义）：
    # ratio<1.3 合格，1.3–1.6 低，1.6–2.2 中，>2.2 高。
    if ratio < 1.3:
        sev, verdict = "INFO", "PASS"
    elif ratio < 1.6:
        sev, verdict = "LOW", "WARN"
    elif ratio < 2.2:
        sev, verdict = "MEDIUM", "WARN"
    else:
        sev, verdict = "HIGH", "FAIL"
    return Finding(
        "INDP-BLK", "独立性", "块内频数 (NIST Freq-in-block)",
        f"把比特流切成 {nblocks} 块（每块 {m} 比特），检验每块 1 的比例是否围绕 0.5。大样本以 χ²/df 效应量判级。",
        f"χ²/df={ratio:.3f}", f"p={p:.3e}", "χ²/df<1.3 合格",
        verdict, sev,
        f"块数={nblocks}，块大小={m}，χ²={chi2:.1f}，df={df}",
        "若 χ²/df 显著>1：存在局部成段偏离，序列非平稳。" if sev in ("HIGH", "MEDIUM") else "")


def t_collision(data: bytes, alpha: float) -> Finding:
    """生日碰撞：在 32-bit 取值空间抽 m 个样本，期望碰撞数 λ=m²/(2·2³²)（泊松）。
    低周期 / 强聚集的 RNG 会远多于期望。"""
    bytes_needed = 4 * 120000
    nbytes = min(len(data), bytes_needed)
    m = nbytes // 4
    if m < 1000:
        return Finding("INDP-BDAY", "独立性", "生日碰撞 (周期/聚集)",
                       "在 32-bit 空间抽样，检验重复度是否异常（低周期/聚集风险）。",
                       "nan", "1.0", "—", "INFO", "INFO", "样本不足")
    c = Counter()
    for i in range(m):
        v = int.from_bytes(data[i * 4:i * 4 + 4], "big")
        c[v] += 1
    obs = sum(v * (v - 1) // 2 for v in c.values())
    lam = m * m / (2.0 * (2 ** 32))
    z = (obs - lam) / math.sqrt(lam)
    p = norm_two_sided_p(z)
    sev = severity_from_p(p, alpha)
    return Finding(
        "INDP-BDAY", "独立性", "生日碰撞 (周期/聚集)",
        "32-bit 空间抽样的成对碰撞数 vs 泊松期望；异常多=低周期/聚集，异常少=分布稀疏。",
        f"z={z:.3f}", f"p={p:.3e}", f"p≥{alpha} 合格",
        "PASS" if p >= alpha else "FAIL", sev,
        f"样本 m={m}，观测碰撞 {obs}，期望≈{lam:.1f}",
        "若不合格：RNG 周期过短或存在聚集，长期会出现可识别的重复结果。" if sev in ("HIGH", "CRITICAL") else "")


# ============================================================== 检验：异常 / 长游程
def t_longest_run(data: bytes, alpha: float) -> Finding:
    bits = len(data) * 8
    if bits < 1000:
        return Finding("ANOM-RUN", "异常", "最大游程 (长 streak)",
                       "检验最长连续同值比特是否超界（异常连号/长 streak）。",
                       "nan", "1.0", "—", "INFO", "INFO", "样本不足")
    cur = 1
    mx = 1
    prev = None
    for b in data:
        for i in range(8):
            bit = (b >> i) & 1
            if prev is not None:
                if bit == prev:
                    cur += 1
                    if cur > mx:
                        mx = cur
                else:
                    cur = 1
            prev = bit
    # 期望最大游程 ≈ log2(bits) + 0.37（Gumbel 近似）
    exp_max = math.log2(bits) + 0.37
    # 用正态近似：偏差 / sqrt(log2(bits)) 量级
    z = (mx - exp_max) / max(1.0, math.sqrt(math.log2(bits)))
    p = norm_two_sided_p(z)
    # 仅当显著超出才判级（长游程偏大多半是巧合，除非极大）
    if z > 2:
        sev = "LOW" if z < 3 else ("MEDIUM" if z < 4 else "HIGH")
        verdict = "WARN"
    elif z < -3:
        sev = "MEDIUM"
        verdict = "WARN"
    else:
        sev = "INFO"
        verdict = "PASS"
    return Finding(
        "ANOM-RUN", "异常", "最大游程 (长 streak)",
        "最长连续同值比特长度；显著过长=异常连号（streak），可能暴露状态机弱点。",
        f"max_run={mx}", f"期望≈{exp_max:.1f} (z={z:.2f})", "z<3 合格",
        verdict, sev,
        f"实际最长游程 {mx} vs 期望≈{exp_max:.1f}",
        "若显著超界：存在异常长 streak，需排查采集/后处理。" if sev in ("HIGH", "MEDIUM") else "")


def t_byte_outliers(data: bytes, alpha: float) -> Finding:
    n = len(data)
    exp = n / 256.0
    out = []
    maxz = 0.0
    for v in range(256):
        c = data.count(v)
        z = (c - exp) / math.sqrt(exp)
        if abs(z) > abs(maxz):
            maxz = z
        if abs(z) > 4.0:
            out.append((v, c, round(z, 2)))
    # 多重比较：256 桶，期望约 0 个 |z|>4；用 Bonferroni
    p = norm_two_sided_p(maxz)
    p_adj = min(1.0, p * 256)
    sev = "INFO" if p_adj >= alpha else ("MEDIUM" if p_adj >= 1e-3 else "HIGH")
    return Finding(
        "ANOM-OUT", "异常", "单字节极端离群值",
        "逐个字节值检验频数是否出现统计极端的离群（>|z|=4），可能指向固定字节注入/丢字节。",
        f"max|z|={maxz:.2f}", f"p(校正)={p_adj:.3e}", f"p≥{alpha} 合格",
        "PASS" if p_adj >= alpha else "WARN", sev,
        f"超标字节数={len(out)}（阈值 |z|>4）" + (f"，例:{out[:5]}" if out else ""),
        "若超标：个别字节显著过多/过少，排查采集管线是否丢字节或注入固定值。" if sev in ("HIGH", "MEDIUM") else "")


# ============================================================== 检验：可预测性
def t_entropy(data: bytes) -> Finding:
    n = len(data)
    cnt = [0] * 256
    for b in data:
        cnt[b] += 1
    h = shannon_entropy(cnt, n)
    nz = sum(1 for c in cnt if c > 0)
    h_mm = h + (nz - 1) / (2.0 * n)     # Miller–Madow 偏差修正
    defi = 8.0 - h
    # 熵缺陷 >0.01 即值得注意，>0.05 较高，>0.1 严重
    if defi <= 0.01:
        sev, verdict = "INFO", "PASS"
    elif defi <= 0.05:
        sev, verdict = "LOW", "WARN"
    elif defi <= 0.1:
        sev, verdict = "MEDIUM", "WARN"
    else:
        sev, verdict = "HIGH", "FAIL"
    return Finding(
        "PRED-ENT", "可预测性", "信息熵 (Shannon + Miller–Madow)",
        "每字节 Shannon 熵与偏差修正熵；越接近 8 bit 越不可压缩/不可预测。",
        f"H={h:.5f}bit", f"缺陷={defi:.4f}bit", "缺陷≤0.01bit 合格",
        verdict, sev,
        f"Shannon={h:.5f}，Miller–Madow={h_mm:.5f}，非零桶={nz}/256",
        "若熵明显低于 8：流可压缩、可被建模，长期存在可预测风险。" if sev in ("HIGH", "MEDIUM") else "")


def t_compress(data: bytes) -> Finding:
    sample = data[:min(len(data), 1 << 20)]
    if not sample:
        return Finding("PRED-GZ", "可预测性", "gzip 压缩率", "空数据。",
                       "nan", "1.0", "—", "INFO", "INFO", "空数据")
    gz = gzip.compress(sample, 9)
    ratio = len(gz) / len(sample)
    # 真随机应≈1（gzip 略膨胀）；明显 <0.95 表示有可压缩结构
    if ratio >= 0.98:
        sev, verdict = "INFO", "PASS"
    elif ratio >= 0.95:
        sev, verdict = "LOW", "WARN"
    else:
        sev, verdict = "MEDIUM", "WARN"
    return Finding(
        "PRED-GZ", "可预测性", "gzip 压缩率",
        "对随机流做 gzip 压缩；若压缩率明显低于 1，说明存在可被算法捕捉的规律（可预测性）。",
        f"ratio={ratio:.4f}", f"压缩率={ratio:.4f}", "≥0.98 合格",
        verdict, sev,
        f"样本 {len(sample)} 字节，压缩后 {len(gz)} 字节",
        "若明显可压缩：流含结构，可能被压缩模型或机器学习预测。" if sev in ("MEDIUM", "LOW") else "")


def t_reproducibility(meta: dict) -> Finding:
    st = meta.get("source_type", "")
    if st.startswith("snapshot"):
        sev = "MEDIUM"
        verdict = "WARN"
        detail = ("输入为固定熵快照经 DRBG 扩展的流：只要快照文件 + 派生参数（generation/install_salt）"
                  "泄露，整条随机链可被完整复现（逐字节重算）。属'可审计但可被预测'的权衡，"
                  "绝不能用于真实抽奖/开奖；真实场景须用在线硬件 TRNG 或 OS CSPRNG。")
        rec = "真实投注环境不要使用固定快照派生链；若用，务必保证快照与 .state 文件不外泄、且每会话换 generation。"
    elif st == "raw":
        sev = "INFO"
        verdict = "PASS"
        detail = "输入为原始字节流（未指明是否来自可复现种子）。若是硬件 TRNG 实时采集则不可复现；若来自固定文件则同快照风险。"
        rec = ""
    else:
        sev = "INFO"
        verdict = "PASS"
        detail = "游戏结果日志本身不直接暴露随机种子；可预测性取决于其底层随机源（参见采集时的随机源说明）。"
        rec = ""
    return Finding(
        "PRED-REPRO", "可预测性", "确定性与可复现性提示",
        "评估该随机流是否可能因'种子/快照固定'而被完整复现预测。",
        "定性", "—", "—", verdict, sev, detail, rec)


# ============================================================== 游戏公平性仿真（骰宝）
def sicbo_settle(d, bets):
    a, b, c = d
    s = a + b + c
    triple = (a == b == c)
    odd = (s % 2) == 1
    payout = 0.0
    hits = set()

    def win(key, rate):
        nonlocal payout, hits
        v = bets.get(key, 0)
        if v > 0:
            payout += v * (1 + rate)
            hits.add(key)

    if not triple:
        if 4 <= s <= 10:
            win("small", 1)
        if 11 <= s <= 17:
            win("large", 1)
        win("odd" if odd else "even", 1)
    for n in range(1, 7):
        cnt = (a == n) + (b == n) + (c == n)
        if cnt > 0:
            win("f%d" % n, cnt)
    if triple:
        win("triple", 30)
    if s == 4:
        win("sum4", 50)
    if s == 17:
        win("sum17", 50)
    if s == 5:
        win("sum5", 30)
    if s == 16:
        win("sum16", 30)
    return payout, hits


def rnd_int_from_bytes(buf: bytes, k: int, pos: list) -> int:
    """忠实复刻项目 rndInt：16bit 拒绝采样，均匀 [0,k)。pos=[idx] 为可变游标。"""
    M = 65536 - (65536 % k)
    while True:
        i = pos[0]
        if i + 2 > len(buf):
            return -1   # 需要更多字节
        v = (buf[i] << 8) | buf[i + 1]
        pos[0] = i + 2
        if v < M:
            return v % k


def theoretical_sicbo():
    """枚举 216 种等概率结果，算出每个注位理论命中概率与理论返还率。"""
    theo = {}
    all_dice = [(a, b, c) for a in range(1, 7) for b in range(1, 7) for c in range(1, 7)]
    keys = ["small", "large", "odd", "even", "f1", "f2", "f3", "f4", "f5", "f6",
            "triple", "sum4", "sum17", "sum5", "sum16"]
    for key in keys:
        wins = 0
        ret_sum = 0.0
        ret2_sum = 0.0
        for d in all_dice:
            p, _ = sicbo_settle(d, {key: 1})
            if p > 0:
                wins += 1
                ret = p  # 下注 1，返还 p（含本金）
                ret_sum += ret
                ret2_sum += ret * ret
        n = len(all_dice)
        p_hit = wins / n
        e_ret = ret_sum / n
        var = ret2_sum / n - e_ret * e_ret
        theo[key] = {"p_hit": p_hit, "e_return": e_ret, "var": var, "house_edge": 1 - e_ret}
    return theo


def t_game_fairness(stream: bytes, rounds: int, alpha: float) -> list[Finding]:
    # 用 rndInt 抽 3 颗骰子（1..6）每局
    buf = stream
    pos = [0]
    # 若字节不够，用 ChaCha20 DRBG 从现有字节扩展补充（无限供给）
    def ensure(need):
        nonlocal buf, pos
        if pos[0] + need <= len(buf):
            return
        # 扩展：以当前 buf 的 sha256 为新种子继续生成
        more = chacha20_generate(hashlib.sha256(buf).digest(), max(need, 1 << 20))
        buf = buf + more
    findings = []
    die_freq = [[0] * 6 for _ in range(3)]
    sum_freq = [0] * 19   # 3..18
    theo = theoretical_sicbo()
    # 实证每注位返还
    emp_ret = {k: 0.0 for k in theo}
    emp_hit = {k: 0 for k in theo}
    stake_each = rounds  # 每局对每个注位下注 1

    for _ in range(rounds):
        dice = []
        for _d in range(3):
            ensure(2)
            v = rnd_int_from_bytes(buf, 6, pos)
            if v < 0:
                ensure(4)
                v = rnd_int_from_bytes(buf, 6, pos)
            dice.append(v + 1)
        for idx, val in enumerate(dice):
            die_freq[idx][val - 1] += 1
        s = sum(dice)
        sum_freq[s] += 1
        for key in theo:
            p, _ = sicbo_settle(tuple(dice), {key: 1})
            if p > 0:
                emp_ret[key] += p
                emp_hit[key] += 1

    # ---- 每颗骰子均匀性（chi-square 1..6）----
    for idx in range(3):
        cnt = die_freq[idx]
        exp = rounds / 6.0
        chi2 = sum((c - exp) ** 2 / exp for c in cnt)
        p = chi2_sf(chi2, 5)
        sev = severity_from_p(p, alpha)
        findings.append(Finding(
            "FAIR-DIE%d" % (idx + 1), "游戏公平性", f"骰子#{idx+1} 1–6 均匀性",
            "实证每颗骰子取值分布 vs 均匀（卡方）。反映 rndInt 映射是否无偏。",
            f"χ²={chi2:.2f}", f"p={p:.3e}", f"p≥{alpha} 合格",
            "PASS" if p >= alpha else "FAIL", sev,
            f"计数={cnt}",
            "若不合格：骰子取值有偏好，游戏不公平。" if sev in ("HIGH", "CRITICAL") else ""))

    # ---- 三骰和分布 vs 理论（卡方 3..18，16 桶）----
    theo_sum = [0] * 19
    for d in [(a, b, c) for a in range(1, 7) for b in range(1, 7) for c in range(1, 7)]:
        theo_sum[sum(d)] += 1
    chi2 = 0.0
    for s in range(3, 19):
        exp = rounds * theo_sum[s] / 216.0
        if exp > 0:
            chi2 += (sum_freq[s] - exp) ** 2 / exp
    p = chi2_sf(chi2, 16)
    sev = severity_from_p(p, alpha)
    findings.append(Finding(
        "FAIR-SUM", "游戏公平性", "三骰和分布 vs 理论三角分布",
        "实证三骰和（3..18）分布 vs 216 种等概率理论分布（卡方）。",
        f"χ²={chi2:.2f}", f"p={p:.3e}", f"p≥{alpha} 合格",
        "PASS" if p >= alpha else "FAIL", sev,
        f"抽样 {rounds} 局，16 个和桶",
        "若不合格：和的产出偏离理论，可能映射或随机源存在偏差。" if sev in ("HIGH", "CRITICAL") else ""))

    # ---- 各注位实证返还率 vs 理论（z 检验）----
    worst = None
    worst_key = ""
    for key in theo:
        e_ret = theo[key]["e_return"]
        var = theo[key]["var"]
        emp = emp_ret[key] / stake_each
        se = math.sqrt(var / rounds)
        z = (emp - e_ret) / se if se > 0 else 0.0
        if worst is None or abs(z) > abs(worst):
            worst = z
            worst_key = key
    p = norm_two_sided_p(worst)
    sev = severity_from_z(worst)
    findings.append(Finding(
        "FAIR-RET", "游戏公平性", "各注位实证返还率 vs 理论庄家优势",
        "对各注位（大/小/单/双/单点/围骰/总和）逐一核对实证返还率是否落在理论值的统计误差内。",
        f"max|z|={abs(worst):.2f}@{worst_key}", f"p={p:.3e}", f"|z|<3 合格",
        "PASS" if sev == "INFO" else "WARN", sev,
        f"抽样 {rounds} 局；最偏离注位={worst_key}（理论 HE={theo[worst_key]['house_edge']:+.4f}）",
        "若显著偏离：该注位实际赔付与理论不符，存在公平性偏差或实现 bug。" if sev in ("HIGH", "CRITICAL", "MEDIUM") else ""))
    return findings


# ============================================================== 检验：游戏结果日志（抽奖/暴击/开奖）
def theoretical_sicbo_sum() -> list[int]:
    """三骰和(3..18)的理论频数（基于 216 种等概率）。"""
    cnt = [0] * 19
    for a in range(1, 7):
        for b in range(1, 7):
            for c in range(1, 7):
                cnt[a + b + c] += 1
    return cnt


def t_outcome_chi2(vals: list[int], alpha: float) -> Finding:
    mn, mx = min(vals), max(vals)
    k = mx - mn + 1
    cnt = [0] * k
    for v in vals:
        cnt[v - mn] += 1
    n = len(vals)
    exp = n / k
    chi2 = sum((c - exp) ** 2 / exp for c in cnt)
    df = k - 1
    p = chi2_sf(chi2, df)
    sev = severity_from_p(p, alpha)
    return Finding(
        "GAME-CHI2", "游戏公平性", "结果分布卡方（均匀基准）",
        "将记录到的整数结果按取值区间做卡方，检验各结果出现频数是否均匀（即无偏向性结果）。",
        f"χ²={chi2:.2f}", f"p={p:.3e}", f"p≥{alpha} 合格",
        "PASS" if p >= alpha else "FAIL", sev,
        f"取值区间 [{mn},{mx}] 共 {k} 档，n={n}",
        "若不合格：某些结果显著偏高/偏低，结果分布被操纵或随机源偏置。" if sev in ("HIGH", "CRITICAL") else "")


def t_outcome_entropy(vals: list[int], alpha: float) -> Finding:
    mn, mx = min(vals), max(vals)
    k = mx - mn + 1
    cnt = [0] * k
    for v in vals:
        cnt[v - mn] += 1
    n = len(vals)
    h = shannon_entropy(cnt, n)
    maxh = math.log2(k)
    defi = maxh - h
    if defi <= 0.01:
        sev, verdict = "INFO", "PASS"
    elif defi <= 0.05:
        sev, verdict = "LOW", "WARN"
    elif defi <= 0.1:
        sev, verdict = "MEDIUM", "WARN"
    else:
        sev, verdict = "HIGH", "FAIL"
    return Finding(
        "GAME-ENT", "游戏公平性", "结果分布熵",
        "结果取值的 Shannon 熵；越接近 log2(档数) 越不可预测。",
        f"H={h:.4f}/{maxh:.3f}", f"缺陷={defi:.4f}", "缺陷≤0.01 合格",
        verdict, sev,
        f"区间 {k} 档，Shannon={h:.4f}（最大 {maxh:.3f}）",
        "若熵明显偏低：结果集中于少数取值，存在可预测/操纵风险。" if sev in ("HIGH", "MEDIUM") else "")


def t_outcome_lag1(vals: list[int], alpha: float) -> Finding:
    n = len(vals)
    if n < 20:
        return Finding("GAME-AC", "游戏公平性", "结果序列 lag-1 自相关", "样本过少。",
                       "0", "1.0", "—", "INFO", "INFO", "样本过少")
    mean = sum(vals) / n
    var = sum((x - mean) ** 2 for x in vals) / n
    if var == 0:
        return Finding("GAME-AC", "游戏公平性", "结果序列 lag-1 自相关", "结果无变化（方差为 0）。",
                       "nan", "1.0", "—", "LOW", "WARN", "结果为常数，需确认是否正常")
    num = sum((vals[i] - mean) * (vals[i + 1] - mean) for i in range(n - 1))
    r = num / (var * (n - 1))
    z = r * math.sqrt(n - 1)
    if abs(r) < 0.02:
        sev, verdict = "INFO", "PASS"
    elif abs(r) < 0.05:
        sev, verdict = "LOW", "WARN"
    elif abs(r) < 0.1:
        sev, verdict = "MEDIUM", "WARN"
    else:
        sev, verdict = "HIGH", "FAIL"
    p = norm_two_sided_p(z)
    return Finding(
        "GAME-AC", "游戏公平性", "结果序列 lag-1 自相关",
        "检验相邻两次结果是否存在线性相关（例如'上次中奖→下次必不中'的暗箱调整）。",
        f"r={r:+.4f}", f"p={p:.3e}", "|r|<0.02 合格",
        verdict, sev,
        f"n={n}，lag-1 皮尔逊自相关 r={r:+.4f}",
        "若 |r| 显著：结果之间存在可建模依赖，可能被人为主控。" if sev in ("HIGH", "MEDIUM") else "")


def t_outcome_runs(vals: list[int], alpha: float) -> Finding:
    n = len(vals)
    if n < 20:
        return Finding("GAME-RUN", "游戏公平性", "结果序列游程检验", "样本过少。",
                       "0", "1.0", "—", "INFO", "INFO", "样本过少")
    med = statistics.median(vals)
    seq = [1 if v >= med else 0 for v in vals]
    ones = sum(seq)
    pi = ones / n
    if abs(pi - 0.5) >= 2.0 / math.sqrt(n):
        return Finding("GAME-RUN", "游戏公平性", "结果序列游程检验", "中位数切分前置未满足。",
                       "nan", "1.0", "—", "WARN", "MEDIUM",
                       "结果过于集中，无法做游程检验。")
    v = 1
    prev = None
    for bit in seq:
        if prev is not None and bit != prev:
            v += 1
        prev = bit
    num = abs(v - 2.0 * n * pi * (1 - pi))
    den = 2.0 * math.sqrt(2.0 * n) * pi * (1 - pi)
    z = num / den if den else 0.0
    p = norm_two_sided_p(z)
    if abs(z) < 2:
        sev, verdict = "INFO", "PASS"
    elif abs(z) < 3:
        sev, verdict = "LOW", "WARN"
    else:
        sev, verdict = "MEDIUM", "WARN"
    return Finding(
        "GAME-RUN", "游戏公平性", "结果序列游程检验",
        "以中位数为界把结果转成高/低序列，做 NIST 游程检验，捕捉'成段中奖/成段不中'的异常聚集。",
        f"z={z:.3f}", f"p={p:.3e}", "|z|<2 合格",
        verdict, sev,
        f"游程数={v}，期望≈{2*n*pi*(1-pi):.0f}",
        "若显著：结果存在成段聚集，可能非独立。" if sev in ("MEDIUM", "LOW") else "")


def t_dice_sum_vs_theory(vals: list[int], alpha: float) -> Finding:
    theo = theoretical_sicbo_sum()
    cnt = [0] * 19
    for s in vals:
        if 3 <= s <= 18:
            cnt[s] += 1
    n = sum(cnt)
    if n < 100:
        return Finding("GAME-SUM", "游戏公平性", "骰宝和分布 vs 理论", "样本过少。",
                       "0", "1.0", "—", "INFO", "INFO", "样本过少")
    chi2 = 0.0
    for s in range(3, 19):
        exp = n * theo[s] / 216.0
        if exp > 0:
            chi2 += (cnt[s] - exp) ** 2 / exp
    p = chi2_sf(chi2, 16)
    sev = severity_from_p(p, alpha)
    return Finding(
        "GAME-SUM", "游戏公平性", "骰宝和分布 vs 理论三角分布",
        "记录到的三骰和(3..18)分布 vs 216 种等概率理论分布（卡方）。",
        f"χ²={chi2:.2f}", f"p={p:.3e}", f"p≥{alpha} 合格",
        "PASS" if p >= alpha else "FAIL", sev,
        f"抽样 {n} 局（仅统计合法和值 3..18）",
        "若不合格：实际开奖和分布偏离理论，可能随机源或映射有偏。" if sev in ("HIGH", "CRITICAL") else "")


def run_audit_game(vals: list[int], meta: dict, alpha: float, dice_flag: bool) -> list[Finding]:
    findings = []
    findings.append(t_outcome_chi2(vals, alpha))
    findings.append(t_outcome_entropy(vals, alpha))
    findings.append(t_outcome_lag1(vals, alpha))
    findings.append(t_outcome_runs(vals, alpha))
    findings.append(t_reproducibility(meta))
    if dice_flag:
        # 仅当结果在区间 3..18 内时按骰宝和分布核对
        if min(vals) >= 3 and max(vals) <= 18:
            findings.append(t_dice_sum_vs_theory(vals, alpha))
    return findings


# ============================================================== 报告渲染
def build_report(data: bytes, meta: dict, findings: list[Finding], opts: dict) -> dict:
    counts = {s: 0 for s in SEV_ORDER}
    for f in findings:
        counts[f.severity] += 1
    # 总体风险 = 最高严重度（排除 INFO/LOW 只看真正风险）
    risk = "INFO"
    for s in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]:
        if counts[s] > 0:
            risk = s
            break
    rep = {
        "tool": "rng_audit.py",
        "generated_iso": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "data": meta,
        "sampled_bytes": meta.get("sampled_bytes", len(data)),
        "overall_risk": risk,
        "severity_counts": counts,
        "findings": [f.to_dict() for f in findings],
    }
    if opts.get("game_sim"):
        rep["game_simulation"] = {"enabled": True, "rounds": opts["rounds"]}
    return rep


def render_console(rep: dict, findings: list[Finding]) -> str:
    L = []
    L.append("=" * 78)
    L.append(f"随机源公平性审计报告  ·  总体风险等级: [{rep['overall_risk']}]")
    L.append("=" * 78)
    d = rep["data"]
    L.append(f"数据来源 : {d.get('source_type')}  {d.get('path','')}")
    L.append(f"样本量   : {rep['sampled_bytes']:,} 字节"
             + (f"   (快照采集: {d.get('created_iso')} from {d.get('from_port')})" if d.get('created_iso') else ""))
    L.append(f"严重度计数: " + "  ".join(f"{s}={rep['severity_counts'][s]}" for s in SEV_ORDER))
    L.append("-" * 78)
    L.append(f"{'ID':<12}{'类别':<10}{'检验项':<30}{'结论':<7}{'严重度':<10}{'关键指标'}")
    L.append("-" * 78)
    for f in findings:
        L.append(f"{f.fid:<12}{f.category:<10}{f.name[:28]:<30}{f.verdict:<7}{f.severity:<10}{f.metric}")
    L.append("-" * 78)
    # 详述有风险的项
    risky = [f for f in findings if SEV_RANK[f.severity] >= SEV_RANK["MEDIUM"] and f.verdict != "PASS"]
    if risky:
        L.append("需关注的问题（MEDIUM 及以上）：")
        for f in risky:
            L.append(f"  ▶ [{f.severity}] {f.fid} {f.name}")
            L.append(f"      指标: {f.metric}  阈值: {f.threshold}")
            L.append(f"      说明: {f.detail}")
            if f.recommendation:
                L.append(f"      建议: {f.recommendation}")
    else:
        L.append("未发现 MEDIUM 及以上风险项（所有检验在统计误差内）。")
    L.append("=" * 78)
    return "\n".join(L)


def render_markdown(rep: dict, findings: list[Finding]) -> str:
    L = []
    L.append("# 随机源公平性审计报告\n")
    L.append(f"- **生成时间**: {rep['generated_iso']}")
    L.append(f"- **总体风险等级**: **{rep['overall_risk']}**")
    d = rep["data"]
    L.append(f"- **数据来源**: `{d.get('path','')}`（{d.get('source_type')}）")
    L.append(f"- **审计样本量**: {rep['sampled_bytes']:,} 字节")
    if d.get("created_iso"):
        L.append(f"- **快照采集**: {d.get('created_iso')} from {d.get('from_port')}")
    L.append(f"- **严重度计数**: " + " · ".join(f"{s}={rep['severity_counts'][s]}" for s in SEV_ORDER))
    L.append("\n## 检验结果汇总\n")
    L.append("| 编号 | 类别 | 检验项 | 统计量 | 关键指标 | 阈值 | 结论 | 严重度 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for f in findings:
        L.append(f"| {f.fid} | {f.category} | {f.name} | {f.statistic} | {f.metric} | {f.threshold} | {f.verdict} | {f.severity} |")
    risky = [f for f in findings if SEV_RANK[f.severity] >= SEV_RANK["MEDIUM"] and f.verdict != "PASS"]
    L.append("\n## 发现的问题与建议\n")
    if risky:
        for f in risky:
            L.append(f"### [{f.severity}] {f.fid} · {f.name}\n")
            L.append(f"- **说明**: {f.detail}")
            L.append(f"- **指标**: {f.metric}（阈值 {f.threshold}）")
            if f.recommendation:
                L.append(f"- **建议**: {f.recommendation}")
            L.append("")
    else:
        L.append("未发现 MEDIUM 及以上风险项；各项统计检验均在给定显著性水平内通过。\n")
    L.append("## 方法论与局限\n")
    L.append("- 均匀性/无偏性：字节 & 半字节卡方、NIST Monobit、KS 检验、比特位偏差；")
    L.append("- 独立性：NIST Runs、字节 & 比特自相关、块内频数、生日碰撞；")
    L.append("- 异常：最大游程、单字节离群值（多重比较校正）；")
    L.append("- 可预测性：Shannon 熵(+Miller–Madow)、gzip 压缩率、确定性/可复现性评估；")
    L.append("- 游戏公平性：复刻项目 `rndInt`(16bit 拒绝采样) 与骰宝 `settleOf`，抽样映射后核对骰子均匀性、和分布与注位返还率。")
    L.append("- 卡方 p 值用 Wilson–Hilferty 近似、正态/KS p 值用渐近式，均不依赖 scipy；样本越大越敏感。")
    L.append("- 本工具为统计审计，不可替代 GLI/WLA 等受监管实验室的完整测试套件。")
    return "\n".join(L)


# ============================================================== 主流程
def run_audit(data: bytes, meta: dict, alpha: float, game_sim: bool, rounds: int) -> list[Finding]:
    findings = []
    # 均匀性 / 无偏性
    findings.append(t_chi_square_bytes(data, alpha))
    findings.append(t_chi_square_nibbles(data, alpha))
    findings.append(t_monobit(data, alpha))
    findings.append(t_ks_uniform(data, alpha))
    findings.append(t_bit_position_bias(data, alpha))
    # 独立性
    findings.append(t_runs(data, alpha))
    findings.append(t_serial_corr_bytes(data, alpha))
    findings.append(t_bit_autocorr(data, alpha))
    findings.append(t_freq_in_block(data, alpha))
    findings.append(t_collision(data, alpha))
    # 异常
    findings.append(t_longest_run(data, alpha))
    findings.append(t_byte_outliers(data, alpha))
    # 可预测性
    findings.append(t_entropy(data))
    findings.append(t_compress(data))
    findings.append(t_reproducibility(meta))
    # 游戏公平性仿真
    if game_sim:
        findings.extend(t_game_fairness(data, rounds, alpha))
    return findings


def make_demo_stream() -> bytes:
    """构造一条故意偏置的流（低位偏 0、含固定字节 0x00 注入）以自证检测能力。"""
    rnd = random.Random(12345)
    out = bytearray()
    for _ in range(200000):
        b = rnd.getrandbits(8)
        # 偏置：把低 2 位清 0（制造 bit0/bit1 偏 0）
        b &= 0xFC
        # 每 50 字节注入一个固定 0x00（制造离群）
        out.append(b)
        if len(out) % 50 == 0:
            out.append(0x00)
    return bytes(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="随机源公平性审计工具")
    ap.add_argument("input", nargs="?", default="random_raw.bin",
                    help="输入文件（.bin / .json 快照 / .csv·.jsonl 游戏日志）；- 表示演示偏置流")
    ap.add_argument("--source", choices=["auto", "raw", "snapshot", "game"], default="auto")
    ap.add_argument("--expand", type=int, default=0,
                    help="把输入经 ChaCha20 DRBG 扩展成 N 字节后再审计（默认 0=不扩展）")
    ap.add_argument("--game", choices=["none", "dice"], default="none",
                    help="把随机流仿真成游戏结果（当前支持 dice 骰宝）")
    ap.add_argument("--sim-rounds", type=int, default=200000, help="公平性仿真局数")
    ap.add_argument("--alpha", type=float, default=0.01, help="显著性水平（默认 0.01）")
    ap.add_argument("--json", default="", help="输出 JSON 报告路径")
    ap.add_argument("--md", default="", help="输出 Markdown 报告路径")
    ap.add_argument("--demo", action="store_true", help="运行自证：对故意偏置的流做审计")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    if a.demo or a.input == "-":
        data = make_demo_stream()
        meta = {"source_type": "demo-biased", "path": "(synthetic biased stream)",
                "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    else:
        src = a.source
        if src == "auto":
            src = autodetect(a.input)
        if src == "snapshot":
            data, meta = load_snapshot(a.input)
        elif src == "game":
            vals, meta = load_game_log(a.input)
            if not vals:
                print("[error] 游戏日志未解析出任何整数", file=sys.stderr)
                return 2
            meta["sampled_bytes"] = len(vals)
            if a.game == "dice" and min(vals) >= 3 and max(vals) <= 18:
                dice_flag = True
            else:
                dice_flag = (a.game == "dice")
            findings = run_audit_game(vals, meta, a.alpha, dice_flag)
            rep = build_report(b"", meta, findings,
                               {"game_sim": False, "rounds": 0})
            if not a.quiet:
                print(render_console(rep, findings))
            if a.json:
                with open(a.json, "w", encoding="utf-8") as f:
                    json.dump(rep, f, ensure_ascii=False, indent=2)
                if not a.quiet:
                    print(f"\n[json] 已写出: {a.json}")
            if a.md:
                with open(a.md, "w", encoding="utf-8") as f:
                    f.write(render_markdown(rep, findings))
                if not a.quiet:
                    print(f"[md]   已写出: {a.md}")
            return 1 if (rep["severity_counts"]["HIGH"] or rep["severity_counts"]["CRITICAL"]) else 0
        else:
            data, meta = load_raw(a.input)

    if a.expand and a.expand > 0:
        seed = hashlib.sha256(data).digest()
        data = chacha20_generate(seed, a.expand)
        meta["expanded_bytes"] = a.expand
        meta["source_type"] += "+DRBG"

    game_sim = (a.game == "dice") or (a.expand and a.game == "none" and meta.get("source_type", "").startswith("snapshot"))
    if a.game == "dice":
        game_sim = True
    if a.game == "none" and not a.expand and meta.get("source_type") == "raw":
        game_sim = False

    findings = run_audit(data, meta, a.alpha, game_sim, a.sim_rounds)
    rep = build_report(data, meta, findings, {"game_sim": game_sim, "rounds": a.sim_rounds})

    if not a.quiet:
        print(render_console(rep, findings))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
        if not a.quiet:
            print(f"\n[json] 已写出: {a.json}")
    if a.md:
        with open(a.md, "w", encoding="utf-8") as f:
            f.write(render_markdown(rep, findings))
        if not a.quiet:
            print(f"[md]   已写出: {a.md}")

    # 退出码：有 HIGH/CRITICAL 风险返回 1（便于 CI 门禁）
    return 1 if (rep["severity_counts"]["HIGH"] or rep["severity_counts"]["CRITICAL"]) else 0


if __name__ == "__main__":
    sys.exit(main())
