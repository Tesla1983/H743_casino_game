# firmware/ —— STM32H743 随机源固件（随包发布）

老虎机只认一条协议：`$?PING` / `$?RND,<n>`，由一块 **STM32H743VIT6** 经 CH340（COM9）
提供。这个目录放进三样东西，让本工程**不依赖另一个仓库也能交付**。

```
firmware/
├── rng_dongle/                     最小固件工程（源 + 链接脚本 + 测试 + 烧录脚本）
├── prebuilt/rng_dongle-1.0.bin     ← 直接烧这个即可（2 988 B，同源码编出）
├── prebuilt/rng_dongle-1.0.elf     带符号，便于 SWD 调试 / nm 查地址
└── backup/ymgui-h743-full-*.bin    回刷用的完整 GUI 固件镜像（1.69 MB）
```

## 三种用法

| 我要做什么 | 命令 |
|---|---|
| 把板子刷成随机源 | `bash firmware/rng_dongle/flash_dongle.sh` |
| 把板子恢复成 GUI 演示机 | `bash firmware/rng_dongle/flash_dongle.sh --restore` |
| 改固件后重新构建 | 见下方「构建」 |
| 验收（6 组断言） | `py -3.12 firmware/rng_dongle/test_dongle.py` |

烧录用 FireDAP（`0416:5021`，SWD）+ `probe-rs`；脚本已包含 **download 后必须 reset**
这一步（不 reset 内核停在 halt，固件不跑，表现为"烧成功但串口无响应"）。

## 构建（需要 ymgui-h743 仓库）

共享代码不复制，`-DYMGUI_ROOT` 指过去：

```bash
cmake -S firmware/rng_dongle -B build-dongle -G Ninja \
      -DCMAKE_MAKE_PROGRAM="E:/software/Clion/CLion 2025.2.5/bin/ninja/win/x64/ninja.exe" \
      -DCMAKE_TOOLCHAIN_FILE="E:/ymgui-h743/cmake/arm-none-eabi.cmake" \
      -DYMGUI_ROOT="E:/ymgui-h743"
cmake --build build-dongle
```

产物 `build-dongle/rng_dongle.bin` 应与 `firmware/prebuilt/rng_dongle-1.0.bin`
**逐字节一致**；不一致说明源码变过，重新验收后再替换 prebuilt。

## 单一真源与同步校验

| 文件 | 真源 | 说明 |
|---|---|---|
| `rng_service.c` / `rng_service.h` | **只有** `E:/ymgui-h743/src/` | 协议、头帧 XOR、SECS/CECS 恢复，本工程不复制 |
| `startup_gcc.s` / `third_party/` | **只有** `E:/ymgui-h743/` | 启动向量表与 CMSIS/HAL 头 |
| `main_dongle.c` / 垫片头 / `.ld` | 两处各一份副本 | 改一处必须同步另一处 |
| `CMakeLists.txt` / `README.md` | 两处**故意不同** | 路径假设不同（这里是"外部 YMGUI_ROOT"） |

同步校验（改了 dongle 文件后跑一次，全静默即一致）：

```bash
for f in src/main_dongle.c include/SYSTEM/usart/usart.h stm32h743_dongle.ld test_dongle.py; do
  diff -q "/e/ymgui-h743/firmware/rng_dongle/$f" "firmware/rng_dongle/$f" || echo "不同步: $f"
done
```

## 协议（与 GUI 固件逐字节一致）

```
$?PING        -> $!PONG
$?RND,<n>     -> $!RND,<n>*HH\r\n + n 字节      HH = 头帧全串（含 '$'）的 XOR
$!ERR                                          SECS/CECS 恢复 4 次仍失败
```

最小固件额外答两条只读诊断（GUI 固件不答，可据此判断刷的是哪版）：

```
$?VER   -> $!VER,RNGDONGLE-1.0
$?STAT  -> $!STAT,<累计字节>,<SECS/CECS 次数>,<开机自检 0|1>
```

桥（`trng_server.py`）与 `slot/entropy.py` **不需要做任何改动**。

## 实测（2026-11-02，COM9 / 115200）

| 项 | 最小固件 | 完整 GUI 固件 |
|---|---|---|
| Flash / 静态 RAM | 2 988 B / 4 312 B | 1 693 428 B / 数十 KB |
| SWD 烧录 | 2.3 s | 225 s |
| 复位 → 应答 `$?PING` | **3 ms** | ≈25 s（等 QSPI 字库） |
| 串口吞吐 | **11.0 KB/s** | 8.1 KB/s |
| 单局 5 B | 4.0 ms（经桥 0.5 ms） | 1.0 ms（经桥） |

200 KB 取样过 `rng_tests.py` 五项全 PASS；长跑 1 003 520 B / 89 s、0 次失败请求、`$?STAT` 的
`errs=0`、`boot=1`。细节与已知边界见 `rng_dongle/README.md`。
