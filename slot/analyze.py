"""TRNG 输出分布分析：字节直方图 + 卡方均匀性检验（含 p 值）。

纯标准库实现，无 numpy 依赖。用于验证一百万（或更多）硬件真随机字节是否
在给定的 bin 数下与均匀分布无显著偏离。
"""

import math
from collections import Counter


def analyze(data: bytes, nbins: int = 256):
    """对 data 做均匀性检验。

    返回 dict: n, counts(list[nbins]), chi2, df, p, mean, minc, maxc。
    p 值用卡方分布的正态近似（df 大时足够准确）。"""
    n = len(data)
    if n == 0:
        raise ValueError("空数据")
    cnt = Counter(data)
    counts = [cnt.get(i, 0) for i in range(nbins)]
    expected = n / nbins
    chi2 = sum((c - expected) ** 2 / expected for c in counts)
    df = nbins - 1
    # 卡方分布正态近似: chi2 ≈ N(df, sqrt(2*df))
    z = (chi2 - df) / math.sqrt(2.0 * df)
    p = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    # 香农信息熵: 越接近 log2(nbins) 越好; 8bit 字节理想值 8.0
    entropy = _entropy(counts, n)
    return {
        "n": n,
        "counts": counts,
        "chi2": chi2,
        "df": df,
        "p": p,
        "entropy": entropy,
        "entropy_max": math.log2(nbins),
        "mean": expected,
        "minc": min(counts),
        "maxc": max(counts),
    }


def _entropy(counts, n):
    h = 0.0
    for c in counts:
        if c:
            pi = c / n
            h -= pi * math.log2(pi)
    return h


def print_report(rep, nbins: int = 256):
    n = rep["n"]
    print(f"样本量        : {n} 字节")
    print(f"卡方统计量 χ² : {rep['chi2']:.3f}  (df={rep['df']})")
    print(f"p 值          : {rep['p']:.4f}")
    print(f"信息熵        : {rep['entropy']:.6f} / {rep['entropy_max']:.4f} bits"
          f"（越接近上限越随机）")
    print(f"各桶期望计数  : {rep['mean']:.2f}   实际 min={rep['minc']} max={rep['maxc']}")
    if 0.01 <= rep["p"] <= 0.99:
        verdict = "✓ 与均匀分布无显著偏离 (PASS)"
    else:
        verdict = "✗ 分布显著偏离均匀 (FAIL)"
    print(f"结论          : {verdict}")
    _print_hist(rep["counts"], nbins)


def _print_hist(counts, nbins):
    m = max(counts) or 1
    cols = 64
    ramp = " .:-=+*#@"
    print("\n字节值直方图 (0..255), 越靠右出现越多:")
    for start in range(0, nbins, cols):
        line = ""
        for v in range(start, min(start + cols, nbins)):
            h = int(counts[v] / m * (len(ramp) - 1))
            line += ramp[h]
        print(f"{start:3d}: {line}")
