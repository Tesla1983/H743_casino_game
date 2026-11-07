# STM32H743 Slot Machine (True-Random Driven)

中文版：[README.md](README.md)

A standalone project that depends only on a single **STM32H743** (CH340 serial, `$?RND,<n>` protocol); it does not depend on any other dev board or project.

Core idea: **The STM32H743 on-chip TRNG provides physical true-random entropy**, and the host side expands it into a cryptographically secure byte stream using **ChaCha20 DRBG**; the game programs (`slot_machine.html` slot machine, `baccarat.html` baccarat, `dice.html` Sic Bo, `blackjack.html` blackjack, `texas_holdem.html` Texas Hold'em) use these bytes to drive the complete game logic — reels / dealing, payout, scoring, and rendering all happen on the host. The true-random source only decides "which cell a reel stops on / which card is dealt"; the symbol distribution and payout table determine the long-term probabilities.

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

This project now includes **five games**, all driven by the same STM32H743 physical true-random source:

- **Slot machine (`slot_machine.html`)**: 5 reels × 3 visible rows per spin (top / middle / bottom), 9 paylines total, with `wild` and `scatter`; reels, payout, scoring, and rendering all run on the host.
- **Baccarat (`baccarat.html`)**: shares the same random chain and the same wallet with this project, also driven by H743 physical entropy.
- **Sic Bo (`dice.html`)**: three dice per round, sharing the same random chain and the same wallet as the other games.
- **Blackjack (`blackjack.html`)**: shares the same random chain and the same wallet as the other games, also driven by H743 physical entropy.
- **Texas Hold'em (`texas_holdem.html`)**: a fifth game category added to this project, sharing the same H743 random chain and the same wallet as the other games.

The only difference between the five games is "how they consume the random bytes" (reel distribution / payout table / dealing rules); **the true-random source only decides "where the next outcome lands / which card is dealt", while the long-term probability is determined by the game rules and is independent of the random source**.

### True-Random Driven: Why the H743 Hardware TRNG

During early development we researched and compared multiple entropy sources (hardware TRNG / host OS CSPRNG / preset random seed tables; the OS CSPRNG was evaluated for comparison only and is not part of the runtime chain), and finally chose the **on-chip TRNG of the STM32H743** as the physical entropy source. Core advantages:

- **True physical entropy, not algorithmic pseudo-randomness**: The H743 TRNG is a hardware peripheral sampling an on-chip **analog ring oscillator**; the entropy comes from physical noise and is theoretically **unpredictable and non-reproducible**; this is distinct from browser `Math.random` or any "seed table" (with the seed you can replay the entire stream).
- **Built-in hardware health self-test**: The firmware reads the status register on every sample to check **SECS / CECS** (seed error / clock error). Once set, it recovers via a "disable-delay-enable" cycle, discards the first two words, and retries; it only reports an error after 4 consecutive failures — **guaranteeing at the hardware level that the bytes emitted truly come from a healthy entropy source**.
- **Independent of the host**: The TRNG is a dedicated chip peripheral, unaffected by host CPU load, OS state, or process scheduling. **Offline operation does not fall back to the host OS CSPRNG either** — this project uses an **offline sample demo mechanism**: H743 physical entropy is captured into sample files beforehand, and the offline bridge reads only those samples and expands them into a random stream with the ChaCha20 DRBG. It never occupies the serial port, coexists with the online bridge, and runs the full demo with no board attached. A fixed sample means a reproducible sequence, so this path is for demos / auditable replay only, never the sole entropy source for a real draw.
- **Cryptographically secure expansion**: The physical entropy is expanded into a usable byte stream by **ChaCha20 DRBG** (RFC 8439); each block output is **rekeyed** (having later output does not reveal earlier output), and every 8192 B it reseeds with freshly derived entropy (forward secrecy); **entropy source and expansion responsibilities are separated**.
- **Statistical quality up to standard**: On 200k–1M bytes, the five tests — 256-bin chi-square / mono-bit / NIST runs / serial correlation / poker — **all pass** (α=0.01), aligned with common items of GLI-11 / NIST SP 800-22.

In one sentence: **The H743 provides "real, healthy, independent" physical entropy, and the host-side DRBG expands it into a safely usable random stream** — this is the fundamental reason this architecture chose a hardware TRNG over a pure software solution.

#### Horizontal Selection: The Lottery-Class-Device Perspective

The core requirement of lottery-class devices such as lottery machines and slot machines is **unpredictable outcomes + uniform distribution** — so that every participant has equal odds and neither the player nor the operator can manipulate the result. This is fundamentally different from financial cryptography or key generation: such devices usually **do not need the "full entropy" bar** (every bit carrying 100% true randomness). A hardware RNG that passes standard statistical randomness tests and shows no obvious pattern or bias is sufficient (except in strictly regulated gambling equipment, which may carry mandatory certification).

Against this backdrop, re-evaluating four common MCUs with a TRNG:

| Chip | Random-number quality (key evidence) | Fit for lottery-class devices |
| :--- | :--- | :--- |
| **STM32H743** | NIST official "full entropy" certification (1 bit/bit) | **Quality ceiling, with performance to spare**. Its RNG quality far exceeds typical needs, providing the most solid fairness foundation; the official docs list "random-number lottery" among its applications. If budget and dev resources allow, choosing it means no randomness problems at all. |
| **ESP32-S3** | Very high measured entropy (≈7.9998 bit/Byte), no official full-entropy cert. | **A strong candidate with excellent cost-performance**. Its physical entropy quality is more than enough for lottery use, and the integrated Wi-Fi/Bluetooth simplifies networking and remote management, sharply cutting system cost and complexity. |
| **RA6E2** | Clearly quantified entropy (0.872315 bit/bit), with an internal evaluation report | **A balanced choice that fits the function**. The official material lists "lottery-class devices" as a typical TRNG application, with data-backed reliable randomness; as an entry-level MCU it may also have a cost edge. |
| **RP2350** | Has a dedicated TRNG, but little public quantified-entropy data | **An option needing extra validation**. Community discussion notes its ROSC-based scheme may yield lower-quality randomness and needs careful configuration; for scenarios with explicit fairness requirements, choosing it means taking on more verification work yourself. |

**Selection advice**: the logic should shift from "pursuing the absolute highest quality" to "finding the best balance of cost, integration, and development convenience while meeting the fairness bar."

- **The most unassailable fairness endorsement** (commercial / regulated environments): **STM32H743VIT6** remains the safest — its NIST full-entropy certification is the ultimate answer to any challenge about randomness quality, and this project falls in this category.
- **High cost-performance + system integration** (smart lottery machines needing networking / remote management): **ESP32-S3** is a strong contender — its measured randomness fully meets the need, and the built-in wireless greatly simplifies the design.
- **Cost-sensitive + needing clear, data-backed reliability**: **RA6E2** fits well — officially recommended for lottery devices, with quantified entropy sufficient to prove fairness.

> **Key reminder**: regardless of the chip, the industry best practice for formal lottery products is to **use the hardware TRNG as a seed to drive a software CSPRNG, and periodically reseed with fresh TRNG data** — guaranteeing randomness quality, uniformity, and generation speed at once. This project's ChaCha20 DRBG is exactly that approach (physical entropy → DRBG expansion → periodic reseeding).

Two access methods: **Online** — send `$?RND` to the board in real time to fetch physical entropy; **Offline** — the **offline sample demo mechanism**: H743 physical entropy is captured into sample files beforehand, and the offline bridge reads only the file, does not occupy the serial port, and can coexist with the online bridge. Two kinds of samples are available: `random_raw.bin`, shipped with the repository (1 MB of true-random bytes sampled from the H743 hardware TRNG, split into 256-byte segments that feed the seed pool + ChaCha20 DRBG — **a full demo works with no board and no on-site entropy sampling**), and `h743_entropy.json` (an entropy snapshot taken by `h743_snapshot.py` while the serial port is idle; `start_casino.py` keeps it valid and re-samples it while the serial port is idle when missing / older than 30 days / failing verification, otherwise falling back to the `random_raw.bin` seed pool). **The default chain has no OS CSPRNG fallback**: when both the online and offline sources are unavailable, the round is cancelled and the wager refunded in full — the host OS CSPRNG is never silently substituted. Both paths finally converge on the same unbiased mapping (`byte % 64`), making no difference to game probabilities.

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
