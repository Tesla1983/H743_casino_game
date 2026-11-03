# rng_dongle —— STM32H743 RNG 最小固件

> **这份是随老虎机工程发布的副本**（2026-11-02 从 `E:/ymgui-h743/firmware/rng_dongle` 复制）。
> 共享代码（`rng_service.c` / `startup_gcc.s` / `third_party`）**没有**复制过来，仍在
> ymgui-h743 维护一份，构建时用 `-DYMGUI_ROOT=<路径>` 指过去；本目录只放
> dongle 专属文件。两边一致性校验见 `firmware/README.md`。
> 只想烧板子、不想构建 → 用 `firmware/prebuilt/rng_dongle-1.0.bin`（就是这里源码编出来的那份）。

只做一件事：**上电即在 USART1(COM9) 上应答 `$?PING` / `$?RND,<n>`**，供老虎机项目
当随机源。

它不是新写的协议栈——`$?RND` 的实现直接复用主工程 **`src/rng_service.c`**（协议、XOR 头帧、
SECS/CECS「关-延-开」恢复全部原样），只是把"谁提供那一行命令"从 vendor 的 `usart.c`
换成一份 ~60 行的裸寄存器 USART1 驱动。

| | 完整 GUI 固件 | 本固件 |
|---|---|---|
| Flash | 1 693 428 B | **2 988 B** |
| RAM（静态） | 数十 KB（heap1 + 帧缓冲） | **4 312 B** |
| SWD 烧录耗时 | 实测 **225 s** | **2.3 s** |
| 复位 → 能应答 `$?PING` | **≈25 s**（等 QSPI 字库启动） | **3 ms**（实测） |
| 依赖 | 屏 / ES8311 / TF 卡 / ESP32 / QSPI 字库 | 只有 USART1 + RNG |
| 串口吞吐 | 实测 8.1 KB/s（主循环还要跑 GUI） | **实测 11.0 KB/s**（接近 115200 上限 11.5） |

## 协议

与 GUI 固件**逐字节一致**（所以桥、`slot/entropy.py`、`tools/h743_rng.py` 都不用改）：

```
主机 → 板子
  $?PING\r\n                探测
  $?RND,<n>\r\n             请求 n 字节（1..4096）

板子 → 主机
  $!PONG\r\n
  $!RND,<n>*HH\r\n + n 字节    HH = "$!RND,<n>" 全串（含 '$'）的 XOR，2 位大写 hex
  $!ERR\r\n                   SECS/CECS 恢复 4 次仍失败
```

本固件额外提供两条**只读诊断**命令（GUI 固件不答，可据此区分刷的是哪一版）：

```
$?VER   -> $!VER,RNGDONGLE-1.0
$?STAT  -> $!STAT,<累计字节>,<SECS/CECS 次数>,<开机自检 0|1>
```

- `$?RND` 的 XOR 口径**含 `$`**，与 RA6E2 的「只算 payload」相反，算错会永远校验失败。
- 开机自检：连续取 32 字节，若全相同则置 0（真随机出现该情况的概率约 2^-248），
  用于"源卡死但仍有响应"这类静默故障。

## 构建

在**老虎机仓库根目录**：

```bash
export PATH="/e/software/ArmGNUToolchain/14.3 rel1/bin:/e/software/Clion/CLion 2025.2.5/bin/ninja/win/x64:$PATH"
cmake -S firmware/rng_dongle -B build-dongle -G Ninja \
      -DCMAKE_MAKE_PROGRAM="E:/software/Clion/CLion 2025.2.5/bin/ninja/win/x64/ninja.exe" \
      -DCMAKE_TOOLCHAIN_FILE="E:/ymgui-h743/cmake/arm-none-eabi.cmake" \
      -DYMGUI_ROOT="E:/ymgui-h743"
cmake --build build-dongle
```

产物：`build-dongle/rng_dongle.bin`（2 988 B）、`.elf`、`.map`。
构建结果应与 `firmware/prebuilt/rng_dongle-1.0.bin` **逐字节一致**（同工具链同参数）；
不一致说明 ymgui-h743 里的共享代码或本目录的 dongle 文件被改过，先查清再烧。

## 烧录 / 回刷

```bash
# 一条命令搞定（自动选本地构建产物 / prebuilt，并跟一次 reset）
bash firmware/rng_dongle/flash_dongle.sh            # 最小固件
bash firmware/rng_dongle/flash_dongle.sh --restore  # 回刷完整 GUI 固件

# 等价的手工命令（FireDAP / SWD）
probe-rs download --probe 0416:5021 --chip STM32H743VITx firmware/prebuilt/rng_dongle-1.0.bin
probe-rs reset    --probe 0416:5021 --chip STM32H743VITx    # download 后内核仍是 halt
```

`firmware/backup/SHA256SUMS.txt` 里是该镜像的 sha256（`698107f0…26cc`）；
老虎机仓库的 `firmware/backup/` 里也放了一份同样的镜像，两边任选其一。
**这块板刷了最小固件后就没有 GUI 了**，要恢复演示能力只能靠这个镜像回刷。

## 验收

```bash
py -3.12 firmware/rng_dongle/test_dongle.py        # 6 组断言（延迟/版本/取数/计数/吞吐/取样）
py -3.12 rng_tests.py _dongle_sample.bin          # 五项统计（本仓库根）
```

2026-11-02 实测（COM9 / 115200）：

| 项 | 结果 |
|---|---|
| open → 首帧 `$!PONG` | **3 ms** |
| `$?VER` | `$!VER,RNGDONGLE-1.0` |
| `$?RND` 1/5/64/512/4096 | XOR 与长度全通过 |
| 单局 5 B（50 次） | 中位 **4.0 ms**（直连串口往返；经桥带缓冲为 0.5 ms） |
| 200 KB 取样 | 17.8 s，**11.0 KB/s**，`$?STAT` → `205664,0,1` |
| `rng_tests.py` 五项 | 全 PASS（χ²=280.3 p=0.133；单比特 p=0.025；runs p=0.346；序列相关 p=0.181；扑克 p=0.406） |
| 桥联调（`trng_server.py`） | `ok=true`、`port=H743:COM9`、缓冲满、`reconnects=1` |

## 已知边界（别当 bug 查）

1. **没做 SECS/CECS 故障注入**：错误恢复分支沿用主工程已验证的代码，但本固件没在硬件上
   人为置位验证过；要验需要在 `rng_service_bytes()` 里临时注入，或在 SWD 里写 `RNG->SR`。
2. **系统时钟故意保持复位默认**（HSI 64 MHz，APB2 /1）。RNG 自己的时钟是 HSI48，与系统
   时钟无关；不配 PLL/HSE 是为少一个「晶振不起振就卡死」的故障面。**改了时钟必须重测串口
   波特率**（`BRR = PCLK2/baud`，代码里 `PCLK2_HZ` 是常量）。
3. **没有看门狗**。主循环是纯轮询，没有阻塞路径；但若将来加入阻塞初始化，必须同时启用 IWDG。
4. **CH340 的 DTR 接 BOOT0**：开串口必须 `dtr=True, rts=False` **再** `open()`，
   否则会拉低 NRST / 进 ISP，表现为"板子不响应"。
5. `startup_gcc.s` 里 `JPEG_IRQHandler` 是强引用（GUI 固件要用硬件 JPEG），本固件给了空实现；
   以后向量表再加强引用项而本固件没定义，链接会直接报未定义符号（不会静默跑飞）。
