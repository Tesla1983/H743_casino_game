#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""start_casino.py — 一键启动：拉起两个 H743 随机源桥 + 打开老虎机页面。

两个源（都由 STM32H743 的物理熵派生，互不抢串口）：
  在线 127.0.0.1:8765  H743 板子经 CH340 串口直供（trng_server.py --source h743）
  离线 127.0.0.1:8770   H743 熵快照 + ChaCha20 DRBG（--offline --entropy file）

用法（双击即可）：
    py start_casino.py
    py start_casino.py --no-browser   # 只起桥、不开页面（联调用）

它做了什么：
  1) 准备 H743 熵快照（没有 / 超 30 天 / 损坏时自动重采；重采需串口空闲）；
  2) 起在线桥（--source h743）与离线桥（--offline --entropy file），各自后台线程
     维护串口连接，掉线自动重连；
  3) 等两个桥 /status 都通，打印各自的熵链路状态；
  4) 用默认浏览器打开 slot_machine.html（页面自动探测 8765/8770 并区分两种模式）；
  5) 作为看守进程保持前台运行；Ctrl+C 一并结束两个桥。

为什么不给离线桥也连板子：
  串口是独占资源，在线桥已经长期占用板载串口。离线桥若也去开串口必然
  PermissionError，所以它只读熵快照——这也正是"离线"的含义。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import webbrowser

BASE = os.path.dirname(os.path.abspath(__file__))
BRIDGE_HOST = "127.0.0.1"
ONLINE_PORT = 8765
OFFLINE_PORT = 8770
PAGE = os.path.join(BASE, "slot_machine.html")
# 页面静态服务端口：**必须走 http**，不能让浏览器用 file:// 打开。
# 原因：Firefox / Zen 对每个 file:// 页面分配独立源（privacy.file_unique_origin），
# 于是 slot_machine.html 与 baccarat.html 各自一份 localStorage —— 积分/银行卡在
# 两个游戏之间根本传不过去（Chrome 内核恰好共享，极易误判成"代码没生效"）。
# 同一个 http 源下两页共用一份存储，跨页 storage 事件也能正常触发。
PAGE_PORT = 8788
PAGE_PORT_TRIES = 20                  # 端口被占用时依次往后找
BRIDGE_SCRIPT = os.path.join(BASE, "trng_server.py")
SNAPSHOT = os.path.join(BASE, "h743_entropy.json")
SNAPSHOT_MAX_AGE_H = 24 * 30          # 超过 30 天建议重采（物理熵统计特性无从复核）


def log(*a):
    print("[start_casino]", *a, flush=True)


# ---------------- 探测 ----------------
def bridge_up(port):
    try:
        with urllib.request.urlopen(
            f"http://{BRIDGE_HOST}:{port}/status", timeout=0.4
        ) as r:
            return r.status == 200
    except Exception:
        return False


def bridge_status(port):
    try:
        with urllib.request.urlopen(
            f"http://{BRIDGE_HOST}:{port}/status", timeout=0.8
        ) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except Exception:
        return None


def h743_present():
    """H743 的 CH340（1A86:7523）是否接入。只查设备表，不开口（避免干扰已运行的桥）。"""
    try:
        import serial.tools.list_ports as lp
    except Exception:
        return False
    for p in lp.comports():
        hw = (p.hwid or "").upper()
        if ("VID_1A86" in hw and "PID_7523" in hw) or "VID:PID=1A86:7523" in hw:
            return True
    return False


# ---------------- 熵快照 ----------------
def ensure_snapshot():
    """确保有一份可用的 H743 熵快照；返回 True 表示离线桥可以走快照模式。"""
    sys.path.insert(0, BASE)
    from slot.entropy import capture_snapshot, load_snapshot

    if os.path.isfile(SNAPSHOT):
        try:
            _raw, doc = load_snapshot(SNAPSHOT)
            age_h = (time.time() - float(doc["created"])) / 3600.0
            if age_h <= SNAPSHOT_MAX_AGE_H:
                log(f"熵快照可用：{os.path.basename(SNAPSHOT)} "
                    f"{doc['bytes']} B（{doc['created_iso']} 采自 {doc['port']}，{age_h:.1f} 小时前）")
                return True
            log(f"熵快照已 {age_h/24:.0f} 天，重新采集…")
        except Exception as e:
            log(f"熵快照不可用（{e}），重新采集…")

    if bridge_up(ONLINE_PORT) or bridge_up(OFFLINE_PORT):
        log("⚠ 有桥在运行（串口被占），无法采新快照；本次离线源退回种子池单源。")
        return False
    if not h743_present():
        log("⚠ 未发现 H743 的 CH340（1A86:7523），也没有可用快照；离线源退回种子池单源。")
        return False
    try:
        capture_snapshot(SNAPSHOT, n=4096, log=log)
        return True
    except Exception as e:
        log(f"⚠ 采快照失败：{e}；离线源退回种子池单源。")
        return False


# ---------------- 起桥 ----------------
def start_bridge(port, extra_args=()):
    args = [sys.executable, BRIDGE_SCRIPT, "--port", str(port), *extra_args]
    log("启动：" + " ".join(os.path.basename(a) if a == BRIDGE_SCRIPT else a for a in args[1:]))
    return subprocess.Popen(args, cwd=BASE, stdin=subprocess.DEVNULL)


def wait_status(port, timeout=20.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if bridge_up(port):
            return True
        time.sleep(0.3)
    return False


def describe(port, kind):
    """把一条链的状态说成人话。"""
    st = bridge_status(port) or {}
    if not st:
        log(f"{kind}：{port} 无响应")
        return
    if kind.startswith("在线"):
        src = st.get("source")
        dev = str(st.get("port") or "").replace("H743:", "")
        if st.get("ok"):
            how = "H743 串口直供"
            log(f"在线源就绪：{how} · {dev or '?'} · HTTP {port}")
        else:
            log(f"在线源未就绪：{st.get('err') or '串口未连'}（HTTP {port} 服务已在，插上即自动重连）")
        return
    d = st.get("drbg") or {}
    ent = st.get("entropy") or {}
    if d.get("certified_only"):
        snap = (ent.get("snapshot") or {})
        log(f"离线源就绪：H743 熵快照 {snap.get('bytes', '?')} B"
            f"（{snap.get('created_iso', '?')} 采自 {snap.get('port', '?')}）"
            f" + ChaCha20 DRBG · 已重播种 {d.get('reseeds', 0)} 次 · HTTP {port}")
        if d.get("reseed_misses"):
            log(f"  ⚠ 有 {d['reseed_misses']} 次重播种因熵不足跳过（不影响出数，可忽略）")
    else:
        sp = st.get("seedpool") or {}
        log(f"离线源就绪：种子池 段 #{sp.get('seg')}（表 {sp.get('table')}）"
            f" + ChaCha20 DRBG · HTTP {port}")


# ---------------- 打开页面 ----------------
def find_zen():
    """定位 Zen 浏览器可执行文件。优先按已知安装路径，再从注册表解析
    系统默认 http 应用指向的可执行文件（绕过 Python webbrowser 误判 Edge 的问题）。"""
    import winreg  # 仅 Windows 有；本脚本只在 Windows 下用于开页面
    candidates = [
        os.environ.get("ProgramFiles", r"C:\Program Files") + r"\Zen Browser\zen.exe",
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)") + r"\Zen Browser\zen.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\ZenBrowser\zen.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Zen Browser\zen.exe"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice",
        ) as k:
            progid = winreg.QueryValueEx(k, "ProgID")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, fr"{progid}\shell\open\command") as k:
            cmd = winreg.QueryValueEx(k, "")[0]
        exe = cmd.split('"')[1] if '"' in cmd else cmd.split()[0]
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    return None


def start_page_server():
    """把整个游戏目录用 http 服务起来，返回 (httpd, url) 或 (None, None)。

    只用 127.0.0.1（不对外），只读静态文件。同源之后两个游戏页才共用一份
    localStorage —— 见上面 PAGE_PORT 的注释。
    """
    import functools
    import http.server
    import threading

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, fmt, *args):      # 静态请求不刷屏，只留错误
            pass

        def end_headers(self):
            # 开发期页面会频繁改，别让浏览器拿缓存：改完刷新就能看到
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

    class QuietServer(http.server.ThreadingHTTPServer):
        # 浏览器预连接 / 页面端口探测 / 无头测试会把刚连上的 TCP 直接 RST 掉
        # （ConnectionResetError 10054），socketserver 默认为每个这种连接打印
        # 一整段堆栈——是"客户端先断开"的正常噪音，不是服务故障，静默吞掉。
        # 其它异常仍按原样上报，便于真问题排查。
        def handle_error(self, request, client_address):
            import sys
            exc = sys.exc_info()[1]
            if isinstance(exc, (ConnectionError, TimeoutError)):
                return
            super().handle_error(request, client_address)

    handler = functools.partial(QuietHandler, directory=BASE)
    for port in range(PAGE_PORT, PAGE_PORT + PAGE_PORT_TRIES):
        try:
            httpd = QuietServer(("127.0.0.1", port), handler)
        except OSError:
            continue
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd, f"http://127.0.0.1:{port}/slot_machine.html"
    return None, None


def open_page(url):
    """用 Zen（或系统默认浏览器）打开页面 URL。

    注意：这里必须传 http:// 地址而不是磁盘路径 —— 见 PAGE_PORT 的注释，
    file:// 在 Firefox 内核下会让两个游戏页变成两个独立源。
    """
    zen = find_zen()
    if zen:
        log(f"用 Zen 浏览器打开：{zen}")
        log(f"页面地址：{url}")
        try:
            subprocess.Popen([zen, url])
            return
        except Exception as e:
            log(f"⚠ 直接启动 Zen 失败：{e}，回退系统默认打开。")
    log(f"打开游戏页面（系统默认浏览器）：{url}")
    if sys.platform == "win32":
        try:
            os.startfile(url)
            return
        except Exception as e:
            log(f"⚠ os.startfile 失败：{e}")
    webbrowser.open(url)


def open_page_file():
    """旧行为：直接把磁盘路径丢给浏览器（file://）。

    仅用于对比排查；这种情况下积分/银行卡在两个游戏之间不共通。
    """
    path = os.path.abspath(PAGE)
    zen = find_zen()
    if zen:
        log(f"用 Zen 浏览器打开（file:// 模式）：{zen}")
        try:
            subprocess.Popen([zen, path])
            return
        except Exception as e:
            log(f"⚠ 直接启动 Zen 失败：{e}，回退系统默认打开。")
    if sys.platform == "win32":
        try:
            os.startfile(path)
            return
        except Exception as e:
            log(f"⚠ os.startfile 失败：{e}")
    webbrowser.open("file:///" + urllib.parse.quote(path.replace("\\", "/")))


# ---------------- 主流程 ----------------
def main():
    no_browser = "--no-browser" in sys.argv[1:]      # 联调/无人值守用：只起桥不开页面
    procs = []

    # 在线桥参数：H743 串口直供（本工程只服务这块板子）
    online_args = ["--entropy", "off"]

    snap_ok = ensure_snapshot()
    offline_args = ["--offline", "--entropy", "file"] if snap_ok else ["--offline", "--entropy", "off"]

    if bridge_up(ONLINE_PORT):
        log(f"在线桥已在 {ONLINE_PORT} 运行，跳过启动。")
    else:
        procs.append(start_bridge(ONLINE_PORT, online_args))
        if not wait_status(ONLINE_PORT):
            log(f"⚠ 在线桥未在预期时间内就绪（{ONLINE_PORT}），请看上方 trng_server.py 输出。")

    if bridge_up(OFFLINE_PORT):
        log(f"离线桥已在 {OFFLINE_PORT} 运行，跳过启动。")
    else:
        procs.append(start_bridge(OFFLINE_PORT, offline_args))
        if not wait_status(OFFLINE_PORT):
            log(f"⚠ 离线桥未在预期时间内就绪（{OFFLINE_PORT}），请看上方 trng_server.py 输出。")

    describe(ONLINE_PORT, "在线源")
    describe(OFFLINE_PORT, "离线源")
    if not h743_present():
        log("提示：未检测到 H743 的 CH340；在线源会一直提示未就绪，离线源不受影响。")

    page_httpd = None
    if no_browser:
        log("--no-browser：跳过打开页面（桥仍在运行）")
    elif "--file" in sys.argv[1:]:
        log("⚠ --file：用 file:// 打开（Firefox 内核下积分不共通，仅排查用）")
        open_page_file()
    else:
        httpd, url = start_page_server()
        if httpd is None:
            log(f"⚠ {PAGE_PORT}~{PAGE_PORT + PAGE_PORT_TRIES - 1} 都被占用，退回 file:// 打开；"
                "此时两个游戏的积分不会共通。")
            open_page_file()
        else:
            page_httpd = httpd
            log(f"页面服务已就绪（127.0.0.1 本机）：{url}")
            log("两个游戏共用同一个源 ⇒ 积分 / 银行卡 / 破产次数真正共通。")
            open_page(url)

    # 没有桥要看守、也没有页面服务 → 真的没事可做
    if not procs and page_httpd is None:
        log("两个桥此前都在运行，本启动器无需看守；可直接关闭此窗口。")
        return
    if not procs:
        # 页面服务在本进程里跑（守护线程），退出会把它一起带走 —— 已打开的页面不受
        # 影响，但「老虎机 ⇄ 百家乐」的跳转会失败，所以必须留在这里。
        log("两个桥已在运行；启动器只负责页面服务，请保持本窗口开启（Ctrl+C 结束）。")
    else:
        log("启动器作为看守进程保持运行；按 Ctrl+C 结束两个桥与启动器。")
    try:
        while True:
            for p in list(procs):
                if p.poll() is not None:
                    log(f"桥进程已退出（PID {p.pid}，退出码 {p.returncode}）。")
                    procs.remove(p)
            if not procs and page_httpd is None:
                log("两个桥都已退出。")
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，正在结束桥…")
        if page_httpd is not None:
            try:
                page_httpd.shutdown()
            except Exception:
                pass
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        log("已结束。")


if __name__ == "__main__":
    main()
