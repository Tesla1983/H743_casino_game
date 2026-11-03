"""STM32H743 硬件 RNG（CH340 / USART1）→ 老虎机 DRBG 的熵源层。

本工程的所有随机性都源自**同一颗 STM32H743 的片上 TRNG**，只是取用方式不同：

    online        芯片 → `$?RND,<n>` → 串口 → 桥缓冲 → 页面（每局实时取数）
    offline file  芯片某刻采 4096 B → 快照文件 → KDF → ChaCha20 DRBG（不占串口）
    offline os    宿主 OS CSPRNG → ChaCha20 DRBG（无板子、无文件）
    offline off   种子池历史表 → ChaCha20 DRBG（可复算，适合回归）

协议（固件实现见 E:/ymgui-h743/src/rng_service.c）：
    主机 → 板子： $?PING
                     探测
                 $?RND,<n>
                  请求 n 字节（1..4096）
    板子 → 主机： $!PONG

                 $!RND,<n>*HH
 + n 字节裸随机
                 $!ERR

  校验：XOR 覆盖 `"$!RND,<n>"` **全串（含 `$`）**，别按"只算 payload"去实现。

串口线序（实测钉死，别按原理图反推）：
    DTR 高 = BOOT0 低 = 正常运行；DTR 低 = 停在 bootloader（不应答）
    RTS 高 = NRST 释放 = 运行；RTS 低 = 复位
  关键技巧：**在 open() 之前**就把 dtr/rts 设成运行态。Windows 打开串口会按当前
  状态断言控制线，若先 open 再改，会有一次 RTS 拉低脉冲 → 板子被复位 → 必须干等
  ~25s（QSPI 字库校验）才能应答。预置后实测 open 到 PONG 只要 10ms。

设计约束：
  - 请求路径（HTTP /rnd）**绝不允许阻塞**：熵由后台线程预先攒进 reservoir，
    `take()` 只从储备里取，取不到就返回 None（调用方降级为单源）。
  - 串口独占：桥运行时长期持有该 CH340 口，同一时刻只能有一个进程用它。
  - 纯标准库 + pyserial（离线/无 H743 时不 import pyserial 也能跑）。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time

try:                                  # 无 pyserial 时仍可 import 本模块（off 模式）
    import serial
    import serial.tools.list_ports as _list_ports
except Exception:                     # pragma: no cover - 取决于运行环境
    serial = None
    _list_ports = None

from .drbg_py import ChaCha20DRBG, mix_seed

CH340_VID = 0x1A86
CH340_PID = 0x7523
BAUD = 115200
REQ_CAP = 4096                        # 与固件 RNG_MAX_REQ 一致

MODE_OFF = "off"
MODE_AUTO = "auto"
MODE_H743 = "h743"
MODE_CERTIFIED = "certified-only"
MODES = (MODE_OFF, MODE_AUTO, MODE_H743, MODE_CERTIFIED)


class EntropyUnavailable(RuntimeError):
    """当前策略要求外部熵，但外部熵不可用。"""


def _xor(data: bytes) -> int:
    x = 0
    for b in data:
        x ^= b
    return x


def candidate_ports(preferred: str | None = None) -> list[str]:
    """候选端口：优先指定口，否则所有 CH340（绝不写死 COM 号）。"""
    if preferred:
        return [preferred]
    if _list_ports is None:
        return []
    out = []
    for p in _list_ports.comports():
        if p.vid == CH340_VID and p.pid == CH340_PID:
            out.append(p.device)
    return out


class H743Link:
    """一条到 H743 RNG 服务的串口链路（按行为识别设备，不认 COM 号）。"""

    def __init__(self, port: str | None = None, timeout: float = 0.6,
                 ping_wait: float = 1.2, log=print):
        self.preferred = port
        self.timeout = timeout
        self.ping_wait = ping_wait
        self.log = log
        self.ser = None
        self.port = None
        self.last_error = ""

    # ---------------- 打开 / 识别 ----------------
    @staticmethod
    def _open_raw(dev: str, timeout: float):
        """打开串口且**不打断**板子运行（先把控制线预置成运行态再 open）。"""
        if serial is None:
            raise EntropyUnavailable("缺少 pyserial，无法打开 H743 串口")
        s = serial.Serial()
        s.port = dev
        s.baudrate = BAUD
        s.timeout = timeout
        s.dsrdtr = False
        s.rtscts = False
        s.dtr = True          # BOOT0 低 = 运行
        s.rts = False         # NRST 高 = 不复位
        s.open()
        return s

    def _ping(self, ser, wait: float) -> bool:
        t_end = time.time() + wait
        while time.time() < t_end:
            try:
                ser.reset_input_buffer()
                ser.write(b"$?PING\r\n")
                line = ser.read_until(b"\r\n")
            except Exception:
                return False
            if b"$!PONG" in line:
                return True
            time.sleep(0.15)
        return False

    def open(self) -> bool:
        """探测并打开；成功返回 True。失败不抛异常（由调用方退避重试）。

        失败时只把原因写进 last_error，由 EntropyManager 统一限流打印 ——
        端口被占用时每 6 s 重试一次，自己打日志会把控制台冲爆。
        """
        ports = candidate_ports(self.preferred)
        if not ports:
            self.last_error = "未发现 CH340 端口（H743 RNG 加密狗未接）"
            return False
        for dev in ports:
            try:
                ser = self._open_raw(dev, self.timeout)
            except Exception as e:              # 被占用 / 拔出 / 驱动异常
                # pyserial 会把 Windows 的 ERROR_ACCESS_DENIED(13) 包进 SerialException，
                # 所以不能只 except PermissionError —— 按 errno / 文本两头认。
                txt = str(e)
                busy = (getattr(e, "errno", None) == 13 or isinstance(e, PermissionError)
                        or "PermissionError(13" in txt or "拒绝访问" in txt
                        or "Access is denied" in txt or "being used by another" in txt)
                if busy:
                    # 串口是独占资源：另一个桥/串口助手开着就会是「拒绝访问」
                    self.last_error = (f"{dev} 被其他程序占用（通常是另一个 trng_server）；"
                                       f"H743 串口独占，两个桥只能有一个混流")
                else:
                    self.last_error = f"{dev} 打不开: {e}"
                continue
            try:
                if self._ping(ser, self.ping_wait):
                    self.ser, self.port = ser, dev
                    self.last_error = ""
                    self.log(f"[entropy] H743 RNG 就绪：{dev}（$!PONG 应答）")
                    return True
                self.last_error = f"{dev} 无 $!PONG 应答（不是 RNG 狗 / 未跑固件 / 在 bootloader）"
            except Exception as e:
                self.last_error = f"{dev} 探测异常: {e}"
                self.log(f"[entropy] {dev} 探测异常: {e}")
            try:
                ser.close()
            except Exception:
                pass
        return False

    # ---------------- 取数 ----------------
    def read(self, n: int, retries: int = 3) -> bytes:
        """向 H743 要 n 字节硬件真随机（阻塞，但每次都有串口超时上限）。"""
        if self.ser is None:
            raise EntropyUnavailable("H743 链路未打开")
        n = max(1, min(int(n), REQ_CAP))
        last = "unknown"
        for _ in range(max(1, retries)):
            try:
                self.ser.reset_input_buffer()
                self.ser.write(b"$?RND,%d\r\n" % n)
                hdr = self.ser.read_until(b"\r\n")
                if b"$!ERR" in hdr:
                    last = "device RNG error"
                    continue
                star = hdr.rfind(b"*")
                if not hdr.startswith(b"$!RND,") or star < 0 or len(hdr) < star + 3:
                    last = f"bad header {hdr!r}"
                    continue
                prefix = hdr[:star]
                try:
                    given = int(hdr[star + 1:star + 3], 16)
                except ValueError:
                    last = "bad checksum hex"
                    continue
                if _xor(prefix) != given:          # H743 口径：含 '$'
                    last = "checksum mismatch"
                    continue
                try:
                    m = int(prefix[len(b"$!RND,"):])
                except ValueError:
                    last = "bad length field"
                    continue
                m = max(1, min(m, REQ_CAP))
                buf = bytearray()
                while len(buf) < m:
                    chunk = self.ser.read(m - len(buf))
                    if not chunk:
                        break
                    buf.extend(chunk)
                if len(buf) == m:
                    return bytes(buf)
                last = f"short payload {len(buf)}/{m}"
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
        raise EntropyUnavailable(f"H743 取熵失败（{last}）")

    def alive(self) -> bool:
        if self.ser is None:
            return False
        try:
            self.ser.write(b"$?PING\r\n")
            if b"$!PONG" in self.ser.read_until(b"\r\n"):
                return True
        except Exception:
            return False
        return False

    def close(self) -> None:
        try:
            if self.ser is not None:
                self.ser.close()
        except Exception:
            pass
        self.ser = None
        self.port = None


class EntropyManager:
    """外部熵池：后台线程维护 H743 链路并把熵攒进 reservoir。

    模式（--entropy）：
      off            不接外部熵（旧行为；DRBG 序列与固件逐比特一致，便于复算审计）
      auto           有就用、没有就单源（不阻塞、不失败；/status 标 degraded）
      h743           必须用：`require_ready()` 等不到就抛 EntropyUnavailable（fail-fast）
      certified-only 认证熵源专属（预留）：只允许物理认证熵源，**不使用固定随机表**，
                     缺熵时不出字节（由上层拒绝服务，绝不静默降级）
    """

    def __init__(self, mode: str = MODE_AUTO, port: str | None = None,
                 pull_chunk: int = 64, reservoir_cap: int = 512,
                 pull_interval: float = 0.8, backoff_max: float = 6.0,
                 log=print):
        if mode not in MODES:
            raise ValueError(f"未知熵源模式 {mode}，可选 {MODES}")
        self.mode = mode
        self.port_arg = port
        self.pull_chunk = int(pull_chunk)
        self.reservoir_cap = max(int(reservoir_cap), self.pull_chunk)
        self.pull_interval = float(pull_interval)
        self.backoff_max = float(backoff_max)
        self.log = log

        self._lock = threading.Lock()
        self._res = bytearray()
        self._stop = threading.Event()
        self._wake = threading.Event()      # 消费端取走字节后唤醒补货（见 take）
        self._thread = None
        self._link: H743Link | None = None
        self._last_fail_msg = ""

        self._st = {
            "mode": mode, "state": "off" if mode == MODE_OFF else "connecting",
            "available": False, "port": "", "bytes_total": 0, "pulls": 0,
            "last_error": "", "last_ok_ts": None, "connect_failures": 0,
            "reservoir": 0,
        }

    # ---------------- 生命周期 ----------------
    def start(self) -> "EntropyManager":
        if self.mode == MODE_OFF:
            self._st["state"] = "off"
            return self
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="entropy", daemon=True)
            self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        self._wake.set()                    # 唤醒可能在 wait 的后台线程
        th, self._thread = self._thread, None
        if th is not None:
            th.join(timeout=2.0)
        link, self._link = self._link, None
        if link is not None:
            link.close()

    # ---------------- 供请求路径使用（非阻塞） ----------------
    def take(self, n: int) -> bytes | None:
        """从储备取 n 字节；不足返回 None（调用方降级为单源，绝不阻塞请求）。"""
        if n <= 0:
            return b""
        with self._lock:
            if len(self._res) < n:
                return None
            out = bytes(self._res[:n])
            del self._res[:n]
            self._st["reservoir"] = len(self._res)
            below = len(self._res) < self.reservoir_cap
        if below:
            # 有人在消费就立刻让后台补货。否则补货线程要等 pull_interval(0.8s) 才醒，
            # 高频消费时储备见底，重播种会被迫跳过。
            self._wake.set()
        return out

    def available(self) -> bool:
        with self._lock:
            return bool(self._st["available"])

    def require_ready(self, n: int = 44, timeout: float = 30.0) -> bytes:
        """阻塞等够 n 字节熵（仅 h743 / certified-only 用），超时抛 EntropyUnavailable。"""
        t_end = time.time() + max(0.0, timeout)
        while time.time() < t_end:
            got = self.take(n)
            if got:
                return got
            time.sleep(0.1)
        raise EntropyUnavailable(
            f"外部熵源不可用（模式 {self.mode}，{timeout:.0f}s 内没拿到 {n} 字节 H743 熵）")

    # ---------------- 状态 ----------------
    def status(self) -> dict:
        with self._lock:
            st = dict(self._st)
            st["reservoir"] = len(self._res)
        st["enabled"] = self.mode != MODE_OFF
        # degraded：启用了外部熵但当前没有 -> 只剩单源出数
        st["degraded"] = bool(st["enabled"] and not st["available"])
        if st.get("last_ok_ts"):
            st["last_ok_age"] = round(time.time() - st["last_ok_ts"], 1)
        else:
            st["last_ok_age"] = None
        st.pop("last_ok_ts", None)
        return st

    # ---------------- 后台线程 ----------------
    def _set(self, **kw) -> None:
        with self._lock:
            self._st.update(kw)

    def _run(self) -> None:
        backoff = 0.5
        while not self._stop.is_set():
            if self._link is None:
                link = H743Link(self.port_arg, log=self.log)
                if link.open():
                    self._link = link
                    self._set(available=True, state="ready", port=link.port or "",
                              last_error="", last_ok_ts=time.time())
                    backoff = 0.5
                else:
                    with self._lock:
                        fails = self._st["connect_failures"] + 1
                    self._set(available=False, state="connecting", connect_failures=fails,
                              last_error=link.last_error or "H743 未应答")
                    # 同一原因只报一次（每 20 次再提醒一下），否则退避重试会把日志刷满
                    msg = self._st["last_error"]
                    if msg != self._last_fail_msg or fails % 20 == 0:
                        self.log(f"[entropy] {msg}（连续 {fails} 次，{backoff:.1f}s 后重试）")
                        self._last_fail_msg = msg
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, self.backoff_max)
                    continue

            # 储备未满才取
            with self._lock:
                need = self.reservoir_cap - len(self._res)
            if need >= self.pull_chunk:
                data = b""
                try:
                    data = self._link.read(self.pull_chunk)
                    with self._lock:
                        room = self.reservoir_cap - len(self._res)
                        if room > 0:
                            self._res.extend(data[:room])
                        self._st["bytes_total"] += len(data)
                        self._st["pulls"] += 1
                        self._st["last_ok_ts"] = time.time()
                        self._st["available"] = True
                        self._st["state"] = "ready"
                        self._st["reservoir"] = len(self._res)
                except Exception as e:
                    # 链路坏了：关掉重连（不退避太久——熵源随时可能被插回来）
                    self._set(available=False, state="error", last_error=str(e))
                    link, self._link = self._link, None
                    if link is not None:
                        link.close()
                    self._stop.wait(1.0)
                    continue

                # 消费端抽得快时（离线 DRBG 每秒几百 KB）储备一直是空的：还在补货就
                # 立刻再取一次，只有取满了才按 pull_interval 慢速巡检。没有这一步，
                # "每 8192 字节重播种"会因为熵供给只有 80 B/s 而几乎全部落空。
                with self._lock:
                    more = self.reservoir_cap - len(self._res)
                if more >= self.pull_chunk and data:
                    continue

            # 等下一次：要么消费者取走字节把 _wake 置位（立即补货），要么到巡检间隔。
            # 先 clear 再 wait，避免 _wake 已置位时空转。
            self._wake.clear()
            self._wake.wait(self.pull_interval)


class WhiteningStage:
    """在线模式的"双源混流"：板子字节 ⊕（H743 熵播种的 ChaCha20 流）。

    XOR 不会破坏均匀性（两路独立均匀流的异或仍均匀），前端 `%64` 映射依旧无偏。
    外部熵没到时**原样放行**（单源），到了再开始混流——两种状态都可用，
    所以不能把"混流开关"当作可用性判据。
    """

    def __init__(self, ent: EntropyManager, reseed_bytes: int = 8192):
        self.ent = ent
        self.reseed_bytes = max(0, int(reseed_bytes))
        self._drbg: ChaCha20DRBG | None = None
        self._used = 0
        self._lock = threading.Lock()
        self.reseeds = 0
        self.reseed_misses = 0      # 到点该重播种但储备里没熵（H743 掉线）的次数
        self.mixed_bytes = 0

    def _ensure(self) -> bool:
        if self._drbg is not None:
            return True
        extra = self.ent.take(44)
        if not extra:
            return False
        self._drbg = ChaCha20DRBG(mix_seed(extra, label=b"slot-whiten-v1"))
        self.reseeds = 1
        return True

    def apply(self, data: bytes) -> bytes:
        if not data:
            return data
        with self._lock:
            if not self._ensure():
                return data                       # 单源直通
            ks = self._drbg.generate(len(data))
            out = bytes(a ^ b for a, b in zip(data, ks))
            self._used += len(data)
            self.mixed_bytes += len(data)
            if self.reseed_bytes and self._used >= self.reseed_bytes:
                extra = self.ent.take(32)
                if extra:
                    self._drbg.reseed(extra)
                    self.reseeds += 1
                    self._used = 0          # 只有真播进去才清零
                else:
                    # 熵断了：不清 _used，下一次 apply 继续尝试（take 只是查储备，不阻塞）。
                    # 计数暴露给 /status，别让"混流还在跑"掩盖"已经不再重播种"。
                    self.reseed_misses += 1
            return out

    def status(self) -> dict:
        with self._lock:
            return {"active": self._drbg is not None, "reseeds": self.reseeds,
                    "reseed_misses": self.reseed_misses,
                    "mixed_bytes": self.mixed_bytes}


# =====================================================================
#  H743 熵快照 —— 离线源（不占串口，可与在线桥并存）
# =====================================================================
#  动机：串口是独占资源，在线桥长期持有 COM9，离线桥若也要连板子播种就会
#  PermissionError。于是把 H743 的熵在"板子空闲时"采成快照文件，离线桥只读文件：
#    - 离线桥与在线桥可同时运行（各自一条独立随机链，都源自同一块 H743 的物理熵）；
#    - 快照 + 派生计数器即可完整复现该链 —— 这正是"离线可审计"的含义，也是
#      它不适合做真实抽奖的原因（文件泄露 = 链可复现）。
#  每次 take() 用 BLAKE2b(快照 ‖ 计数器) 派生**互不相同**的熵，不是重复用同一段字节。

SNAP_VERSION = 1
SNAP_LABEL = b"h743-snapshot-v1"


def capture_snapshot(path: str, n: int = 4096, port: str | None = None, log=print) -> dict:
    """采一份 H743 熵快照（需要板子空闲）。返回写入的元数据。"""
    link = H743Link(port, log=log)
    if not link.open():
        raise EntropyUnavailable(f"H743 打不开，无法采快照：{link.last_error or '未应答'}")
    try:
        data = link.read(max(64, min(int(n), REQ_CAP)))
        used = link.port
    finally:
        link.close()
    doc = {
        "version": SNAP_VERSION,
        "created": time.time(),
        "created_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "port": used,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "entropy_hex": data.hex(),
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    os.replace(tmp, path)                  # 原子落盘，别留下半个文件
    log(f"[snapshot] {path}  {len(data)} B  来自 {used}  sha256 {doc['sha256'][:16]}…")
    return doc


def load_snapshot(path: str) -> tuple[bytes, dict]:
    """读快照并校验完整性（sha256 不符直接拒绝，避免静默用坏文件）。"""
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    if int(doc.get("version", 0)) != SNAP_VERSION:
        raise ValueError(f"快照版本不认：{doc.get('version')!r}")
    raw = bytes.fromhex(doc["entropy_hex"])
    if hashlib.sha256(raw).hexdigest() != doc.get("sha256"):
        raise ValueError("快照 sha256 不符（文件被改过或损坏）")
    if len(raw) != int(doc.get("bytes", -1)):
        raise ValueError("快照长度与元数据不符")
    return raw, doc


def _load_or_init_state(snapshot_path: str) -> tuple[int, bytes]:
    """读/建 `<快照>.state`：启动世代号 + 每份安装唯一的 salt。

    两件事都必须有：

    1) **世代号**：只用快照做 key、计数器从 0 起，会导致每次重启离线桥都从头重放同一条流
       （实测两次启动前 32 字节完全一致）。每次启动 +1 即换链，且知道那一代编号仍可复算。
    2) **安装 salt**：分发时同一份快照会发给所有玩家，若只有「快照 + 世代」，
       每台机器的第 1 代 key 完全相同 —— **所有玩家看到同一副牌**。首次运行生成
       16 字节随机 salt 并持久化，混进 key：同一快照在不同安装上得到不同链。
       代价：想复算必须同时保留 salt（它就在 .state 里，属于"审计材料"）。
    """
    st_path = snapshot_path + ".state"
    doc = {}
    try:
        if os.path.isfile(st_path):
            with open(st_path, "r", encoding="utf-8") as f:
                doc = json.load(f) or {}
    except Exception:
        doc = {}
    try:
        gen = int(doc.get("generation", 0)) + 1
    except Exception:
        gen = 1
    salt_hex = str(doc.get("install_salt") or "")
    if len(salt_hex) != 32:
        salt_hex = os.urandom(16).hex()          # 每份安装唯一
    try:
        tmp = st_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"generation": gen, "install_salt": salt_hex,
                       "updated": time.time(),
                       "updated_iso": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
        os.replace(tmp, st_path)
    except Exception:
        pass          # 写不了就照常跑（最坏：下次重启重复同一代）
    return gen, bytes.fromhex(salt_hex)


class FileEntropy:
    """用 H743 快照当熵源：接口与 EntropyManager 对齐，但**不碰串口**。

    供离线桥使用（`--entropy file --entropy-file h743_entropy.json`）。

    ⚠ 世代号（2026-11-02 修）：只拿快照做 key、计数器从 0 起，会导致**每次重启离线桥
    都从头重放同一条流**（实测两次启动前 32 字节完全一致）。现在把"启动世代"持久化到
    `<快照>.state`，每次启动 +1 并混进 key：
        key = BLAKE2b(快照, person) ‖ generation
    于是重启即换链，但又**可复现**——只要知道当时那一代的 generation 就能重算，
    审计能力不丢（/status.entropy.generation 会报出来）。
    """

    def __init__(self, path: str, log=print):
        self.path = path
        self.log = log
        self.mode = "file"        # 让 _offline_init 的 mode 判定与 EntropyManager 一致
        self._lock = threading.Lock()
        self._ctr = 0
        self._bytes = 0
        self._raw, self.doc = load_snapshot(path)
        self.generation, self._salt = _load_or_init_state(path)
        base = hashlib.blake2b(self._raw, digest_size=64, person=SNAP_LABEL).digest()
        k = hashlib.blake2b(base, digest_size=64)
        k.update(self.generation.to_bytes(8, "big"))
        k.update(self._salt)                     # 安装唯一：同一快照在不同机器上换链
        self._key = k.digest()
        log(f"[entropy] 快照熵源：{os.path.basename(path)}  "
            f"{self.doc['bytes']} B（{self.doc['created_iso']} 采自 {self.doc['port']}）"
            f" · 第 {self.generation} 代 · 安装 {self._salt[:4].hex()}")

    # 与 EntropyManager 对齐的接口
    def take(self, n: int) -> bytes | None:
        if n <= 0:
            return b""
        with self._lock:
            out = bytearray()
            while len(out) < n:
                self._ctr += 1
                blk = hashlib.blake2b(self._key, digest_size=64)
                blk.update(self._ctr.to_bytes(8, "big"))
                out += blk.digest()
            self._bytes += n
            return bytes(out[:n])

    def require_ready(self, n: int = 44, timeout: float = 0.0) -> bytes:
        got = self.take(n)
        if not got:
            raise EntropyUnavailable("快照熵源不可用")
        return got

    def available(self) -> bool:
        return True

    def start(self):                       # 与 EntropyManager 同签名，便于同一处调用
        return self

    def close(self):
        pass

    def status(self) -> dict:
        with self._lock:
            st = {
                "mode": "file", "state": "ready", "available": True, "enabled": True,
                "degraded": False, "port": f"file:{os.path.basename(self.path)}",
                "bytes_total": self._bytes, "pulls": self._ctr,
                "derivations": self._ctr, "generation": self.generation,
                "install": self._salt[:4].hex(),
                "last_error": "",
                "snapshot": {"bytes": self.doc["bytes"],
                             "created_iso": self.doc["created_iso"],
                             "port": self.doc["port"],
                             "sha256": self.doc["sha256"][:16]},
            }
        return st


class OsEntropy:
    """主机操作系统的 CSPRNG（Windows 上即 BCryptGenRandom，经 os.urandom）。

    适用场景：**完全离线、不接任何硬件、也不带种子文件**的游戏 —— 开箱即用。

    与另外两种的分工：
      · H743 在线 / 快照：熵来自那块芯片的物理 TRNG；可复现审计（快照模式）；
      · 本类            ：熵来自宿主操作系统内核 CSPRNG；**不可复现**（没有 seed+counter
                          可以回放某一局），但也不需要任何外设或文件。

    安全性说明：这是内核级密码学随机源（不是 Math.random）。Windows 自 10 起用
    "system-preferred" 实现，熵源包含启动期噪声与硬件 RNG（RDRAND / TPM，若存在）。
    它不会因为"没接开发板"而退化；代价只是事后无法逐字节重算 —— 做审计回放就得
    回到快照模式。
    """

    mode = "os"

    def __init__(self, log=print):
        self.log = log
        self._lock = threading.Lock()
        self._bytes = 0
        log("[entropy] 熵源 = 操作系统 CSPRNG（os.urandom / BCryptGenRandom）；不可复现")

    def take(self, n: int) -> bytes | None:
        if n <= 0:
            return b""
        b = secrets.token_bytes(n)
        with self._lock:
            self._bytes += n
        return b

    def require_ready(self, n: int = 44, timeout: float = 0.0) -> bytes:
        return self.take(n)

    def available(self) -> bool:
        return True

    def start(self):
        return self

    def close(self):
        pass

    def status(self) -> dict:
        with self._lock:
            n = self._bytes
        return {"mode": "os", "state": "ready", "available": True, "enabled": True,
                "degraded": False, "port": "os:urandom", "bytes_total": n,
                "pulls": None, "derivations": None, "last_error": ""}
