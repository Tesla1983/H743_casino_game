"""STM32H743 硬件 RNG -> 本地 HTTP 桥（供 slot_machine.html 取字节）。

本工程只服务 STM32H743 这一块板子（CH340 / USART1，`$?RND,<n>` 协议），
不再依赖任何其他开发板或工程。

用法（在本目录下运行）:
    py -3.12 trng_server.py                       # 默认 127.0.0.1:8765（在线：H743 串口直供）
    py -3.12 trng_server.py --port 8765
    py -3.12 trng_server.py --offline             # 离线：种子池 + ChaCha20 DRBG
    py -3.12 trng_server.py --offline --entropy file   # 离线：H743 熵快照当种子（不占串口）
    py -3.12 trng_server.py --offline --entropy os     # 完全离线：OS CSPRNG 当种子

    端口被占用时会**自动顺延**到下一个空闲端口，并在控制台打印实际端口；
    页面会自动探测到它，无需手工改。

接口:
    GET /rnd?n=5   -> n 字节 application/octet-stream
    GET /status    -> {"ok":true,"port":"H743:COMx","buffered":N,"err":"",
                       "source":"h743","reconnects":0,"last_data_age":0.0,
                       "entropy":{...}, "whiten":null, "drbg":{...}}
    所有响应带 Access-Control-Allow-Origin: *（页面与桥不同端口，需 CORS）

原理（在线）: 按 CH340 VID:PID 认板 -> `$?PING` 探测 -> 后台线程持续 `$?RND,512`
      取字节填环形缓冲 -> HTTP 从缓冲取。断链按指数退避重连，插上即自动上线。

熵源模式（--entropy，详见 slot/entropy.py）:
    off            种子池单源（历史随机表；序列可复算）
    auto           离线时：有 H743 就用、没有就单源
    h743           离线时：必须拿到 H743 熵，否则启动失败
    certified-only 离线：不用随机表，DRBG 只由 H743 熵播种
    file           离线：H743 熵快照 + DRBG（不占串口，可与在线桥并存）
    os             离线：操作系统 CSPRNG + DRBG（无板子、无文件）
"""
import argparse
import json
import os
import sys
import threading
import time
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import slot.entropy as _en_mod     # pyserial 可用性用它的 serial 判定（H743 也要串口）

from slot.drbg_py import SEED_LEN
from slot.entropy import (EntropyManager, WhiteningStage, EntropyUnavailable,
                          FileEntropy, OsEntropy, capture_snapshot, load_snapshot)

CAP = 65536                     # 环形缓冲上限（超出丢最旧，仍为随机字节）
ENTROPY_SEED_WAIT = 3.0         # auto 模式启动时等 H743 熵的上限（等不到就单源起步）
ENTROPY_REQUIRED_WAIT = 30.0    # h743 / certified-only 模式等熵的上限（含板子冷启动）

_buf = bytearray()
_lock = threading.Lock()        # 保护 _buf
_board_lock = threading.Lock()  # 保护 _state / _board 的写
_drbg_lock = threading.Lock()   # 保护离线 DRBG 状态与重播种计数
_board = {"ser": None}          # 当前串口句柄（由 _reader 线程维护，重连时替换）
_state = {"ok": False, "port": "", "err": "", "reconnects": 0, "last_data_age": None}
_stop = threading.Event()       # 退出信号（Event.wait 可被 finally 及时唤醒）

# 离线种子池模式：不连板子，用种子池的一段喂 ChaCha20 DRBG 产随机
_offline = {"on": False, "drbg": None, "pool": None, "seg": None, "err": "",
            "generated": 0, "reseeds": 0, "reseed_misses": 0, "reseed_bytes": 0,
            "entropy_mode": "off", "certified_only": False, "seeded_with_h743": False}

# 在线源固定为 STM32H743（本工程只服务这一块板子）
_source = "h743"

# 外部熵（H743）：ent 负责攒熵，whiten 只用于在线模式的混流白化
_ent: EntropyManager | None = None
_whiten: WhiteningStage | None = None



def _set_state(**kw):
    with _board_lock:
        _state.update(kw)


def _reader_h743(port_arg=None, link_cls=None):
    """在线源 = H743 板子本身：直接 `$?RND` 取字节填缓冲。

    不经任何中间 USB CDC、不做白化混流（H743 就是这个源的物理熵源），
    因此在线模式也不能再启动 EntropyManager——串口同一时刻只能被一个对象持有。
    """
    if link_cls is None:
        from slot.entropy import H743Link
        link_cls = H743Link
    backoff = 0.5
    while not _stop.is_set():
        link = link_cls(port_arg, log=print)
        if not link.open():
            _set_state(ok=False, err=link.last_error or "H743 未应答（$?PING 无 PONG）")
            _stop.wait(backoff)
            backoff = min(backoff * 2, 5.0)
            continue
        with _board_lock:
            _board["link"] = link
            _state.update(ok=True, port=f"H743:{link.port}", err="", last_data_age=0.0)
            _state["reconnects"] = _state.get("reconnects", 0) + 1
        print(f"[bridge] H743 在线源就绪：{link.port}")
        backoff = 0.5
        try:
            while not _stop.is_set():
                with _lock:
                    room = CAP - len(_buf)
                if room < 512:
                    _stop.wait(0.05)          # 缓冲满了：等消费者取走，别空转
                    continue
                # 512 B/次：小包往返开销占比高（64 B 约 3.9 KB/s、512 B 约 8 KB/s），
                # 小包全是往返开销（每次固定 ~16 ms 起）
                data = link.read(512)
                with _lock:
                    _buf.extend(data)
                    if len(_buf) > CAP:
                        del _buf[:len(_buf) - CAP]
                with _board_lock:
                    _state.update(ok=True, err="", last_data_age=0.0)
        except Exception as e:
            with _board_lock:
                _board["link"] = None
                _state.update(ok=False, err=f"{type(e).__name__}: {e}")
            try:
                link.close()
            except Exception:
                pass
            _stop.wait(1.0)


def _offline_init(pool_dir, table, seg_bytes, reseed_bytes):
    """离线模式初始化：种子池取一段 (+H743 熵) -> 初始化 ChaCha20 DRBG。

    种子来源按 --entropy 决定：
      off            -> 只用种子池段（与固件同算法的可复算路径）
      auto / h743    -> KDF(种子池段, H743 熵)；auto 等不到熵就单源起步，
                        h743 等不到就抛错（fail-fast，绝不静默降级）
      certified-only -> 不用随机表，DRBG 只由 H743 硬件熵播种
    """
    from slot.seedpool import SeedPool
    from slot.drbg_py import ChaCha20DRBG, selftest, mix_seed
    if not selftest():
        raise RuntimeError("ChaCha20 自检失败")

    mode = _ent.mode if _ent else "off"
    extra = _take_entropy(mode, ENTROPY_REQUIRED_WAIT if mode in ("h743", "certified-only", "file", "os")
                          else ENTROPY_SEED_WAIT)

    if mode in ("certified-only", "file", "os"):
        if not extra:
            raise EntropyUnavailable(f"{mode} 模式需要外部熵播种，但没拿到")
        drbg = ChaCha20DRBG(extra)
        _offline.update(on=True, drbg=drbg, pool=None, seg=None, err="",
                        generated=0, reseeds=0, reseed_misses=0, reseed_bytes=reseed_bytes,
                        entropy_mode=mode, certified_only=True,
                        seeded_with_h743=(mode != "os"))
        if mode == "file":
            print("[offline] H743 离线源：DRBG 由 H743 熵快照播种（不碰串口、不用随机表）")
        elif mode == "os":
            print("[offline] 完全离线：DRBG 由操作系统 CSPRNG 播种（无板子、无种子文件；不可复现）")
        else:
            print("[offline] certified-only：DRBG 仅由 H743 硬件熵播种（不使用固定随机表）")
        return

    pool = SeedPool(pool_dir, seg_bytes)
    pool.load(table)
    seed, seg = pool.next_seed()
    if extra:
        seed = mix_seed(seed, extra, label=b"slot-seed-pool+h743")
    drbg = ChaCha20DRBG(seed)
    st = pool.status()
    _offline.update(on=True, drbg=drbg, pool=pool, seg=seg, err="",
                    generated=0, reseeds=0, reseed_misses=0, reseed_bytes=reseed_bytes,
                    entropy_mode=mode, certified_only=False, seeded_with_h743=bool(extra))
    print(f"[offline] 表 {st.get('table')} 段 #{seg}（剩余 {st.get('remaining')} 段），"
          f"指纹 {(st.get('sha256') or '')[:16]}…"
          + ("  + H743 熵混种" if extra else "  （单源：种子池）"))


def _take_entropy(mode: str, wait: float):
    """按模式取一段外部熵；auto 失败返回 None，h743/certified-only 失败抛错。"""
    if _ent is None or mode == "off":
        return None
    try:
        return _ent.require_ready(SEED_LEN, timeout=wait)
    except EntropyUnavailable as e:
        if mode in ("h743", "certified-only"):
            raise
        print(f"[entropy] 未取到 H743 熵，降级为单源：{e}")
        return None


def _maybe_reseed():
    """离线 DRBG 按已产字节数定期用新 H743 熵重播种（非阻塞：没熵就跳过）。"""
    o = _offline
    if not o["on"] or not o["reseed_bytes"]:
        return
    if _ent is None or o["entropy_mode"] == "off":
        return
    with _drbg_lock:
        if o["generated"] < o["reseed_bytes"]:
            return
        extra = _ent.take(32)
        if not extra:
            # 熵没攒够：保持原状态继续出数（DRBG 本身安全），但必须留下痕迹——
            # 否则"每 8192 字节重播种"悄悄变成"只播了一次"也看不出来。
            o["reseed_misses"] += 1
            return
        o["drbg"].reseed(extra)
        o["reseeds"] += 1
        o["generated"] = 0



class Handler(BaseHTTPRequestHandler):
    # 浏览器（Zen/Edge）会随时中断已建立的 TCP 连接：刷新页面、切走标签页、
    # 请求超时（前端 fetch 1.5s 超时）、连接池复用失败等，都会让客户端 RST 掉连接。
    # 服务器此时正好在写响应就会抛 ConnectionAbortedError(WinError 10053)。
    # 这是正常的"客户端已断开"，必须静默吞掉——否则会刷满堆栈且毫无意义。
    # 注：刻意保持默认 HTTP/1.0（每请求一连接、Connection: close），避免 keep-alive
    # 复用空闲连接带来的额外抖动；断连异常统一在下面三层防护里静默处理。

    def handle_error(self, e):
        # 注意：socketserver 实际调用的是【服务器类】的 handle_error(request, client_address)，
        # 写在 Handler 上不会被调到（见 BridgeServer.handle_error 的真实现）。
        # 这里保留只是兼容旧签名，真正干活的是 Handler.handle 的 try/except。
        if isinstance(e, (ConnectionAbortedError, BrokenPipeError, OSError)):
            self.close_connection = True
            return
        super().handle_error(e)

    def handle(self):
        # 最后一道防线：任何 escaped 的客户端断开异常都不应冒泡到 socketserver 打印堆栈
        try:
            super().handle()
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            self.close_connection = True

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control", "no-store")

    def _send(self, code, body, ctype):
        try:
            self.send_response(code)
            self._cors()
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)
            self.wfile.flush()
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            # 写到一半客户端断开，正常情况，忽略
            self.close_connection = True

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/status":
            ent = _ent.status() if _ent else None
            if _offline["on"]:
                st = _offline["pool"].status() if _offline["pool"] else {}
                # buffered/reconnects 在离线模式下无意义，但保留为 0，
                # 让前端 probeBridge 与 _is_ours 的字段判定对两种模式都成立
                payload = {"ok": True, "mode": "offline", "buffered": 0, "reconnects": 0,
                           "last_data_age": None, "err": _offline.get("err", ""),
                           "seedpool": {"table": st.get("table"), "seg": _offline["seg"],
                                        "remaining": st.get("remaining"),
                                        "sha256": (st.get("sha256") or "")[:16]},
                           "source": "drbg",
                           "drbg": {"seeded_with_h743": _offline["seeded_with_h743"],
                                    "reseeds": _offline["reseeds"],
                                    "reseed_misses": _offline["reseed_misses"],
                                    "reseed_bytes": _offline["reseed_bytes"],
                                    "certified_only": _offline["certified_only"],
                                    "seed_from": _offline["entropy_mode"]},
                           "entropy": ent}
                self._send(200, json.dumps(payload).encode(), "application/json")
                return
            with _lock:
                n = len(_buf)
            with _board_lock:
                payload = dict(_state)
            payload["mode"] = "online"
            payload["source"] = _source
            payload["buffered"] = n
            payload["entropy"] = ent
            payload["whiten"] = _whiten.status() if _whiten else None
            self._send(200, json.dumps(payload).encode(), "application/json")
            return
        if u.path == "/rnd":
            q = parse_qs(u.query)
            try:
                n = int(q.get("n", ["5"])[0])
            except ValueError:
                n = 5
            n = max(1, min(n, 4096))
            if _offline["on"]:
                with _drbg_lock:
                    out = _offline["drbg"].generate(n)
                    _offline["generated"] += n
                _maybe_reseed()
                self._send(200, out, "application/octet-stream")
                return
            # 在线模式：板子未连上且缓冲不足时立刻 503，
            # 不再白等 2 秒——否则前端每局都要耗满超时才失败，表现为"取数掉线"
            with _board_lock:
                board_ok = _state["ok"]
            with _lock:
                have = len(_buf)
            if not board_ok and have < n:
                self._send(503, json.dumps({"err": "board not connected"}).encode(),
                           "application/json")
                return
            out = bytearray()
            deadline = time.time() + 2.0
            while len(out) < n and time.time() < deadline:
                with _lock:
                    take = min(n - len(out), len(_buf))
                    if take > 0:
                        out += _buf[:take]
                        del _buf[:take]
                if len(out) < n:
                    time.sleep(0.02)
            # 双源混流：⊕ H743 熵播种的白化流（熵没到时原样放行，单源可用）
            data = _whiten.apply(bytes(out)) if _whiten else bytes(out)
            self._send(200, data, "application/octet-stream")
            return
        self._send(404, b"not found", "text/plain")

    def log_message(self, *a):
        pass                                        # 静默，避免刷屏


def _is_our_bridge(host, port, timeout=0.35):
    """探测该端口上跑的是不是我们自己的桥（/status 返回带 buffered 的 JSON）。"""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/status", timeout=timeout) as r:
            j = json.loads(r.read().decode("utf-8", "replace") or "{}")
        return isinstance(j, dict) and "buffered" in j and "reconnects" in j
    except Exception:
        return False


class BridgeServer(ThreadingHTTPServer):
    """Windows 下默认的 SO_REUSEADDR 会允许"抢占"别人已监听的端口，
    导致第二个实例误以为端口空闲、两个桥同时抢同一个串口。这里显式关掉。"""
    daemon_threads = True
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        # 浏览器随时会 RST 掉连接（刷新/预连接/端口探测/超时断开），在 readline
        # 或 finish() 阶段抛 ConnectionResetError(10054)/10053。这是"客户端先断开"
        # 的正常噪音，不是服务故障——socketserver 默认会为每个这种连接打印整段
        # 堆栈，这里静默吞掉；其它异常仍按原样上报，便于真问题排查。
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class BridgeAlreadyRunning(Exception):
    """目标端口上已经有本桥在跑。"""
    def __init__(self, port):
        super().__init__(port)
        self.port = port


def _find_running(host, port, span=5):
    """启动前先看看附近端口上是否已经有本桥（避免重复启动抢串口）。"""
    for p in range(port, port + span + 1):
        if _is_our_bridge(host, p):
            return p
    return None


def _make_server(host, port, span=19):
    """在 [port, port+span] 里找第一个空闲端口绑定。

    端口被占用时不再直接抛异常退出（这正是"双击脚本窗口一闪就关"的原因），
    而是顺延到下一个空闲端口；返回 (server, 实际端口)。
    若冲突端口上其实是本桥的另一个实例，则抛 BridgeAlreadyRunning 交给上层友好退出。
    """
    last = None
    for p in range(port, port + span + 1):
        try:
            return BridgeServer((host, p), Handler), p
        except OSError as e:
            last = e
            if _is_our_bridge(host, p):
                raise BridgeAlreadyRunning(p)
            if p == port:
                why = getattr(e, "strerror", None) or e
                print(f"[warn] 端口 {p} 被占用（{why}），自动顺延查找空闲端口…")
    raise OSError(f"{host}:{port}~{port + span} 全部被占用，最后一次错误：{last}")


def main():
    global _ent, _whiten
    # 被管道/日志文件接走时 stdout 是块缓冲，出事时日志永远是空的；
    # 桥的日志是排查熵源/串口的唯一线索，统一改成行缓冲。
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="STM32H743 RNG HTTP bridge")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--offline", action="store_true",
                    help="离线模式：不连板子，用种子池 + ChaCha20 DRBG 产随机")
    ap.add_argument("--table", default=None,
                    help="离线模式使用的随机表（默认按 state / random_raw.bin）")
    ap.add_argument("--pool-dir", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--seg-bytes", type=int, default=256)
    ap.add_argument("--entropy", default="auto",
                    choices=["off", "auto", "h743", "certified-only", "file", "os"],
                    help="外部熵源（STM32H743 硬件 RNG）策略：auto=有就用（默认），"
                         "h743=必须用，certified-only=只用认证熵源不用随机表，"
                         "file=用 H743 熵快照（离线桥，不占串口），"
                         "os=用操作系统 CSPRNG（完全离线，无板子/无文件），off=不用")
    ap.add_argument("--entropy-file", default="h743_entropy.json",
                    help="--entropy file 时使用的 H743 熵快照（h743_snapshot.py 生成）")
    ap.add_argument("--entropy-reseed-bytes", type=int, default=8192,
                    help="每产出多少字节用新 H743 熵重播种一次（0=只播种不重播种）")
    ap.add_argument("--h743-port", default=None,
                    help="指定 H743 串口（默认按 CH340 VID:PID + $?PING 行为识别）")
    a = ap.parse_args()

    if a.entropy == "os" and not a.offline:
        print("[启动失败] --entropy os 只用于离线桥（在线桥的随机来自板子）。")
        print("  正确用法：py -3.12 trng_server.py --offline --entropy os")
        return 2

    if a.entropy == "file" and not a.offline:
        print("[启动失败] --entropy file 只用于离线桥（在线桥的随机来自板子）。")
        print("  正确用法：py -3.12 trng_server.py --offline --entropy file")
        return 2

    if a.entropy == "certified-only" and not a.offline:
        print("[启动失败] certified-only 只对离线 DRBG 有意义（在线模式的随机来自 H743 板子）。")
        print("  正确用法：py -3.12 trng_server.py --offline --entropy certified-only")
        return 2

    # 启动自检：在线模式要开 H743 的 CH340，必须有 pyserial。缺库时后台线程会
    # 一直失败，页面只能看到"熵源未连"却不知根因——这里早失败、早说清。
    if not a.offline and _en_mod.serial is None:
        print("\n[启动失败] 在线模式无法加载 pyserial。")
        print("  本机 pyserial 装在 Python 3.12，请改用：")
        print("      py -3.12 trng_server.py")
        print("  仅想离线跑（快照/OS 熵源）时加 --offline。")
        return 2

    dup = _find_running(a.host, a.port)
    if dup is not None:
        print(f"[info] {a.host}:{dup} 上已有一个 TRNG 桥在运行，无需重复启动。")
        return 0

    try:
        srv, port = _make_server(a.host, a.port)
    except BridgeAlreadyRunning as e:
        print(f"[info] {a.host}:{e.port} 上已有一个 TRNG 桥在运行，无需重复启动。")
        return 0

    # 外部熵源：离线模式按 --entropy 选择；在线模式不叠加（源就是 H743）
    if a.entropy == "os":
        _ent = OsEntropy(log=print).start()
    elif a.entropy == "file":
        try:
            _ent = FileEntropy(a.entropy_file, log=print).start()
        except Exception as e:
            print(f"[启动失败] 读不到 H743 熵快照 {a.entropy_file}：{e}")
            print("  先采一份（需要板子空闲）：py -3.12 h743_snapshot.py")
            srv.server_close()
            return 1
        print("[entropy] 模式 file：外部熵源 = H743 熵快照（不连串口）")
    elif not a.offline:
        # 在线源本身就是 H743（同一根串口），再开一个 EntropyManager 只会抢不到口
        _ent = None
        print("[entropy] 在线源 = H743 串口直供：不再叠加熵源/白化（同一串口只能一个持有者）")
    else:
        _ent = EntropyManager(a.entropy, port=a.h743_port, log=print).start()
        if a.entropy != "off":
            print(f"[entropy] 模式 {a.entropy}：外部熵源 = STM32H743 硬件 RNG（后台探测中）")

    if port != a.port:
        print(f"[info] 实际监听端口：{port}（原 {a.port} 被占用）")
    if a.offline:
        try:
            _offline_init(a.pool_dir, a.table, a.seg_bytes, a.entropy_reseed_bytes)
        except Exception as e:
            print(f"[offline] 初始化失败: {e}")
            print("  先准备种子池：py -3.12 seedpool_cli.py status / "
                  "refill --from random_raw.bin")
            if _ent is not None:
                _ent.close()
            return 1
        if a.entropy == "os":
            print(f"完全离线桥: http://{a.host}:{port}  (无板子/无种子文件，DRBG 由 OS CSPRNG 播种)")
        elif a.entropy == "file":
            print(f"H743 快照桥: http://{a.host}:{port}  (不占串口，DRBG 由熵快照播种)")
        else:
            print(f"离线种子池桥: http://{a.host}:{port}  (不连板子，DRBG 从种子池扩展)")
    else:
        print(f"H743 bridge: http://{a.host}:{port}  (在线源 = H743 板子经串口直接供数)")
    print("  页面会自动探测该端口；在『随机源』里选择 H743 在线 / H743 离线；Ctrl+C 退出。")

    if not a.offline:
        # 连接全部交给后台线程（启动时没插板也能起服务，之后插上自动连；断线自动重连）
        threading.Thread(target=_reader_h743, daemon=True).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _stop.set()
        with _board_lock:
            ser = _board["ser"]
        if ser is not None:
            try:
                stop_stream(ser)
            except Exception:
                pass
            try:
                ser.close()
            except Exception:
                pass
        if _ent is not None:
            _ent.close()          # 释放 H743 串口（否则别的程序打不开）
        print("\n[exit] 服务已停止。")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except KeyboardInterrupt:
        rc = 0
    except Exception as e:
        # 关键：把异常打印出来并"停住"窗口，避免双击运行时一闪而过、看不到原因
        traceback.print_exc()
        if isinstance(e, ModuleNotFoundError) and "serial" in str(e):
            print("\n[提示] 当前解释器里没有 pyserial。本机 pyserial 装在 Python 3.12，")
            print("       请用：  py -3.12 trng_server.py")
        try:
            input("\n启动失败，按回车键关闭窗口…")
        except Exception:
            pass
        rc = 1
    else:
        # 启动自检失败（如缺 pyserial / 离线初始化失败）已打印提示，停住窗口让用户看到
        if rc not in (0, None):
            try:
                input("\n启动未完成，按回车键关闭窗口…")
            except Exception:
                pass
    sys.exit(rc)
