# STM32H743 Slot Machine (True-Random Driven)

中文版：[README.md](README.md)

A standalone project that depends only on a single **STM32H743** (CH340 serial, `$?RND,<n>` protocol); it does not depend on any other dev board or project.

Core idea: **The STM32H743 on-chip TRNG provides physical true-random entropy**, and the host side expands it into a cryptographically secure byte stream using **ChaCha20 DRBG**; the game programs (`slot_machine.html` slot machine, `baccarat.html` baccarat) use these bytes to drive the complete game logic — reels / dealing, payout, scoring, and rendering all happen on the host. The true-random source only decides "which cell a reel stops on / which card is dealt"; the symbol distribution and payout table determine the long-term probabilities.

---

## 1. STM32H743 Hardware

- **Main MCU**: STM32H743VIT6, with a built-in **TRNG** (True Random Number Generator, based on an analog ring oscillator).
- **Communication**: CH340 serial (USART1), protocol `$?RND,<n>` to supply bytes; `$?PING` / `$!PONG` for board identification. `$?VER` / `$?STAT` are specific to the minimal firmware.
- **Firmware** (minimal firmware `firmware/`, RNG dongle): only does RNG + serial — 2,988 B flash, 2.3 s flash time, 3 ms from reset to `$?PING` response, 11.0 KB/s serial throughput.
- **Opening the serial port does not reset the board**: set `dtr=True, rts=False` before `open()`, to avoid the RTS (NRST) low pulse accidentally resetting the board.
- **Read-only operations**: the bridge only sends `$?PING` / `$?RND,<n>`; it does not write flash, touch option bytes, or toggle BOOT0/NRST.
- **Serial exclusivity**: the bridge holds the CH340 exclusively while running; other programs cannot open it at the same time.

---

## 2. Games

This project now includes **two games**, both driven by the same STM32H743 physical true-random source:

- **Slot machine (`slot_machine.html`)**: 5 reels × 3 visible rows per spin (top / middle / bottom), 9 paylines total, with `wild` and `scatter`; reels, payout, scoring, and rendering all run on the host.
- **Baccarat (`baccarat.html`)**: shares the same random chain and the same wallet with this project, also driven by H743 physical entropy.

The only difference between the two games is "how they consume the random bytes" (reel distribution / payout table / dealing rules); **the true-random source only decides "where the next outcome lands / which card is dealt", while the long-term probability is determined by the game rules and is independent of the random source**.

### True-Random Driven: Why the H743 Hardware TRNG

During early development we researched and compared multiple entropy sources (hardware TRNG / host OS CSPRNG / preset random seed tables), and finally chose the **on-chip TRNG of the STM32H743** as the physical entropy source. Core advantages:

- **True physical entropy, not algorithmic pseudo-randomness**: The H743 TRNG is a hardware peripheral sampling an on-chip **analog ring oscillator**; the entropy comes from physical noise and is theoretically **unpredictable and non-reproducible**; this is distinct from browser `Math.random` or any "seed table" (with the seed you can replay the entire stream).
- **Built-in hardware health self-test**: The firmware reads the status register on every sample to check **SECS / CECS** (seed error / clock error). Once set, it recovers via a "disable-delay-enable" cycle, discards the first two words, and retries; it only reports an error after 4 consecutive failures — **guaranteeing at the hardware level that the bytes emitted truly come from a healthy entropy source**.
- **Independent of the host**: The TRNG is a dedicated chip peripheral, unaffected by host CPU load, OS state, or process scheduling. Offline, the OS CSPRNG can be used as a zero-config fallback, but that is a kernel deterministic algorithm whose "unpredictability" cannot be proven byte-by-byte afterwards.
- **Cryptographically secure expansion**: The physical entropy is expanded into a usable byte stream by **ChaCha20 DRBG** (RFC 8439); each block output is **rekeyed** (having later output does not reveal earlier output), and every 8192 B it reseeds with freshly derived entropy (forward secrecy); **entropy source and expansion responsibilities are separated**.
- **Statistical quality up to standard**: On 200k–1M bytes, the five tests — 256-bin chi-square / mono-bit / NIST runs / serial correlation / poker — **all pass** (α=0.01), aligned with common items of GLI-11 / NIST SP 800-22.

In one sentence: **The H743 provides "real, healthy, independent" physical entropy, and the host-side DRBG expands it into a safely usable random stream** — this is the fundamental reason this architecture chose a hardware TRNG over a pure software solution.

Two access methods: **Online** — send `$?RND` to the board in real time to fetch physical entropy; **Offline** — sample the physical entropy into a snapshot file during idle time, the offline bridge reads only the file, does not occupy the serial port, and can coexist with the online bridge. The repository ships `random_raw.bin` (1 MB, a true-random file sampled from the H743 hardware TRNG), so without a board the offline bridge can directly use this file for a demo, with no on-site entropy sampling needed. Both paths finally converge on the same unbiased mapping (`byte % 64`), making no difference to game probabilities.

---

## 3. Running

```bash
# One-click start (recommended): launch the two H743 random-source bridges + open the game page
py start_casino.py
#   —— Double-click to run. Internally: ① ensure h743_entropy.json is available (re-sample during
#      serial idle if missing / older than 30 days / checksum fails); ② start online bridge 8765
#      (H743 serial direct supply); ③ start offline bridge 8770 (snapshot + DRBG, no serial port);
#      ④ open slot_machine.html; ⑤ act as watchdog (Ctrl+C ends both bridges together).
#      Optional: --no-browser to start bridges only, without opening the page.
```

> System Python 3.12 has pyserial installed (the default `python` is 3.13 and has **no** pyserial), so use 3.12 to run.
