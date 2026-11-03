"""与固件 `ra6e2_drbg.c` 同算法的 ChaCha20 DRBG（Python 版）。

用途：
  - 离线种子池模式：种子池的一段 -> key/nonce -> DRBG 扩展出随机字节；
  - 与固件交叉验证：同一 seed 应产出同一序列，便于事后审计复算。

参数与固件严格一致：
  - ChaCha20 (RFC 8439)，256-bit key + 96-bit nonce + 32-bit block counter
  - block counter 从 1 起（counter=0 专供 rekey）
  - 每次 generate() 结束后 rekey：key = ChaCha20(key, nonce, counter=0)[0:32]
  - 构造时先做一次 rekey（与固件 drbg_reseed 的"白化"一致）

双熵源（B 阶段）：
  - `mix_seed()` 是带域分隔的 BLAKE2b KDF，把「种子池段」与「H743 硬件熵」混成 44 字节种子；
  - `ChaCha20DRBG.reseed(extra)` 在运行中把新熵混进 key/nonce（重播种）。
  ⚠ 只要不调用 mix_seed/reseed，本实现与固件逐比特一致（可事后复算审计）；
    一旦混入外部熵，序列就只由本机 DRBG 状态决定——审计口径见 README「双熵源」。

⚠ 固定 seed 会得到固定序列（确定性）。要"不可预测"，seed 必须保密且每会话不同
   （见 seedpool：每次会话取不同段）。
"""
from __future__ import annotations

import hashlib
import struct

_MASK = 0xFFFFFFFF

SEED_LEN = 44                    # 32-byte key + 12-byte nonce，与固件 drbg 同构


def mix_seed(*parts: bytes, label: bytes = b"slot-drbg-seed-v1") -> bytes:
    """带域分隔的种子混合 KDF：任意多段材料 -> 44 字节种子。

    用 BLAKE2b（stdlib，无需第三方库），每段前加长度前缀避免"拼接歧义"
    （part1=b"ab", part2=b"c" 与 part1=b"a", part2=b"bc" 必须得到不同结果）。
    同一组输入必然得到同一结果 -> 离线可复算，便于审计。
    """
    h = hashlib.blake2b(digest_size=SEED_LEN, person=b"slot-seed-v1")
    h.update(len(label).to_bytes(2, "big"))
    h.update(label)
    for p in parts:
        b = bytes(p)
        h.update(len(b).to_bytes(4, "big"))
        h.update(b)
    return h.digest()


def _rotl(x: int, n: int) -> int:
    return ((x << n) | (x >> (32 - n))) & _MASK


def _qr(s, a, b, c, d):          # quarter-round
    s[a] = (s[a] + s[b]) & _MASK; s[d] = _rotl(s[d] ^ s[a], 16)
    s[c] = (s[c] + s[d]) & _MASK; s[b] = _rotl(s[b] ^ s[c], 12)
    s[a] = (s[a] + s[b]) & _MASK; s[d] = _rotl(s[d] ^ s[a], 8)
    s[c] = (s[c] + s[d]) & _MASK; s[b] = _rotl(s[b] ^ s[c], 7)


def chacha20_block(key: bytes, nonce: bytes, counter: int) -> bytes:
    """生成一个 64 字节 ChaCha20 块（RFC 8439，20 轮）。"""
    if len(key) != 32:
        raise ValueError("key 必须 32 字节")
    if len(nonce) != 12:
        raise ValueError("nonce 必须 12 字节")
    st = [0x61707865, 0x3320646e, 0x79622d32, 0x6b206574]
    st += list(struct.unpack("<8I", key))
    st += [counter & _MASK]
    st += list(struct.unpack("<3I", nonce))
    x = st[:]
    for _ in range(10):                      # 10 double-round = 20 轮
        _qr(x, 0, 4, 8, 12); _qr(x, 1, 5, 9, 13)
        _qr(x, 2, 6, 10, 14); _qr(x, 3, 7, 11, 15)
        _qr(x, 0, 5, 10, 15); _qr(x, 1, 6, 11, 12)
        _qr(x, 2, 7, 8, 13); _qr(x, 3, 4, 9, 14)
    return struct.pack("<16I", *[(x[i] + st[i]) & _MASK for i in range(16)])


class ChaCha20DRBG:
    """与固件同构的 DRBG。seed 至少 44 字节（32 key + 12 nonce）。"""

    def __init__(self, seed: bytes, info: bytes | None = None):
        if len(seed) < SEED_LEN:
            raise ValueError(f"seed 至少 {SEED_LEN} 字节（32 key + 12 nonce）")
        self.key = bytes(seed[:32])
        self.nonce = bytes(seed[32:SEED_LEN])
        self.counter = 1
        self.reseeds = 0
        if info:                             # 可选 personalization（交叉验证时勿传）
            k = bytearray(self.key)
            for i, b in enumerate(info):
                k[i % 32] ^= b
            self.key = bytes(k)
        self._rekey()

    def reseed(self, extra: bytes) -> None:
        """把外部熵（H743 硬件 RNG 字节等）混进当前状态，然后 rekey。

        旧 key/nonce 与新熵一起过 KDF：只要任一路径有真熵，结果就不可预测
        （同时保留前向保密——旧的输出无法从新状态反推）。
        """
        if not extra:
            raise ValueError("reseed 需要非空外部熵")
        s = mix_seed(self.key, self.nonce, bytes(extra), label=b"slot-drbg-reseed-v1")
        self.key, self.nonce = s[:32], s[32:SEED_LEN]
        self.counter = 1
        self.reseeds += 1
        self._rekey()

    def _rekey(self) -> None:
        self.key = chacha20_block(self.key, self.nonce, 0)[:32]
        self.counter = 1

    def generate(self, n: int) -> bytes:
        """生成 n 字节（内部状态推进；每次调用后 rekey，与固件一致）。"""
        if n <= 0:
            return b""
        out = bytearray()
        while len(out) < n:
            out += chacha20_block(self.key, self.nonce, self.counter)
            self.counter = (self.counter + 1) & _MASK
            if self.counter == 0:            # 32-bit 计数回绕兜底（实际到不了）
                self._rekey()
        self._rekey()
        return bytes(out[:n])


def selftest() -> bool:
    """RFC 8439 §2.3.2 测试向量：验证实现正确性。"""
    K = bytes(range(32))
    N = bytes.fromhex("000000090000004a00000000")
    EXP = bytes.fromhex(
        "10f1e7e4d13b5915500fdd1fa32071c4c7d1f4c733c068030422aa9ac3d46c4e"
        "d2826446079faa0914c2d705d98b02a2b5129cd1de164eb9cbd083e8a2503c4e")
    return chacha20_block(K, N, 1) == EXP


if __name__ == "__main__":
    print("RFC8439 selftest:", "PASS" if selftest() else "FAIL")
