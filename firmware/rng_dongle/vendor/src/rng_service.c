/**
 * @file        rng_service.c
 * @brief       STM32H743 硬件 RNG 经 USART1(串口端口) 对外提供真随机源（认证 RNG）
 *
 * ───────────────────────────────────────────────────────────────────────────
 * 协议（行式，CRLF 结束；命令来自主机，响应回 USART1）：
 *
 *   主机 → 板子：   $?RND,<n>\r\n       请求 n 字节真随机（1 ≤ n ≤ RNG_MAX_REQ）
 *                   $?PING\r\n          探测（用于主机按行为识别本设备）
 *
 *   板子 → 主机：   $!RND,<n>*<HH>\r\n   头帧（HH = 头帧 ASCII 的 XOR 校验，2 位 hex）
 *                   <n 字节裸随机>        紧跟其后的定长二进制负载（避免 $/CRLF 歧义）
 *                   $!ERR\r\n            RNG 持续出错（已尝试恢复仍失败）
 *                   $!PONG\r\n           对 $?PING 的应答
 *
 * 校验：主机读头帧到 \r\n，对 "$!RND,<n>" 重算 XOR 比对 HH，再按 n 读定长裸负载。
 *       任何不匹配/超时 → 主机重试。
 *
 * 说明：
 *   - 不改动 vendor 的 usart.c / printf 通道；命令分发只认 g_usart_rx_sta 行完成标志。
 *   - RNG 时钟用 HSI48(48MHz) 作 RNGxCLK（H7 RNG 官方推荐，远高于 CECS 阈值）。
 *   - 每次请求实时读取，绝不预填缓冲反复发同一段。
 *   - 出错（SECS/CECS）走"关-延-开"恢复；仍失败则回 $!ERR 让主机重试。
 * ───────────────────────────────────────────────────────────────────────────
 */

#include <string.h>
#include "rng_service.h"
#include "stm32h7xx_hal.h"          /* 设备头：RNG/RCC 寄存器与 __HAL_RCC_* 宏 */
#include "./SYSTEM/usart/usart.h"   /* USART_UX, USART_REC_LEN */

/* RNG 状态/控制位直接用设备头宏：RNG_SR_DRDY / RNG_SR_CECS / RNG_SR_SECS / RNG_CR_RNGEN
 * （定义在 stm32h743xx.h，均为对应的 _Ms 位掩码）。不要重复 define。 */

/* 诊断量（SWD 可直读，验收用） */
uint64_t g_rng_bytes = 0;
uint32_t g_rng_err   = 0;

/* 静态发送缓冲（响应负载上限 = RNG_MAX_REQ） */
static uint8_t s_obuf[RNG_MAX_REQ];

/* ---------------------------------------------------------------------------
 * 底层：向 USART1 阻塞发送 len 字节（与 vendor fputc 同机制，不碰 HAL 句柄）。
 * 主循环稳态下无 printf，故不会与调试输出竞争 TDR。
 * ------------------------------------------------------------------------- */
static void uart1_send(const uint8_t *p, uint32_t len)
{
    for (uint32_t i = 0; i < len; i++)
    {
        while ((USART_UX->ISR & 0x40U) == 0U) { /* 等待 TXE */ }
        USART_UX->TDR = p[i];
    }
}

/* ---------------------------------------------------------------------------
 * RNG 初始化：使能外设时钟 + HSI48 + 选源 + 使能 RNG + 丢弃初值。
 * 必须在 HAL/clocks 起来之后调用（main 里 usart_init 之后即可）。
 * ------------------------------------------------------------------------- */
void rng_service_init(void)
{
    /* 1) RNG 外设总线时钟 */
    __HAL_RCC_RNG_CLK_ENABLE();

    /* 2) RNG 时钟源选 HSI48：H743 的 RNGSEL 在 RCC->D2CCIP2R（非 RNGCFGR），
     *    用 HAL 宏配置 RNGCLK = HSI48（H7 RNG 官方推荐时钟，远高于 CECS 阈值）。 */
    __HAL_RCC_RNG_CONFIG(RCC_RNGCLKSOURCE_HSI48);

    /* 3) 使能 HSI48 并等待就绪 */
    SET_BIT(RCC->CR, RCC_CR_HSI48ON);
    uint32_t t = 0;
    while ((RCC->CR & RCC_CR_HSI48RDY) == 0U)
    {
        if (++t > 1000000u) break;   /* 防呆：永不死等 */
    }

    /* 4) 使能 RNG */
    RNG->CR |= RNG_CR_RNGEN;

    /* 5) 丢弃上电后前几字（H7 勘误：使能后首字可能非随机） */
    for (int i = 0; i < 4; i++)
    {
        uint32_t w = 0;
        while (!(RNG->SR & RNG_SR_DRDY))
        {
            if (++w > 1000000u) break;
        }
        (void)RNG->DR;
    }
}

/* ---------------------------------------------------------------------------
 * 填充 out[0..n) 为硬件真随机字节。
 * 返回 0=成功，-1=出现 RNG 错误（已尝试恢复仍失败）。
 * ------------------------------------------------------------------------- */
int rng_service_bytes(uint8_t *out, uint32_t n)
{
    uint32_t produced = 0;
    uint32_t recoveries = 0;

    while (produced < n)
    {
        uint32_t sr = RNG->SR;

        /* 种子/时钟错误：计数 → 关-延-开 恢复 → 丢两字 → 重试（上限防呆） */
        if (sr & (RNG_SR_SECS | RNG_SR_CECS))
        {
            g_rng_err++;
            RNG->CR &= ~RNG_CR_RNGEN;
            for (volatile uint32_t d = 0; d < 2000; d++) { /* 短延时 */ }
            RNG->CR |= RNG_CR_RNGEN;
            for (int i = 0; i < 2; i++)
            {
                uint32_t w = 0;
                while (!(RNG->SR & RNG_SR_DRDY))
                {
                    if (++w > 1000000u) break;
                }
                if (RNG->SR & RNG_SR_DRDY) (void)RNG->DR;
            }
            if (++recoveries > 4u) return -1;
            continue;
        }

        /* 等待数据就绪（带超时） */
        uint32_t to = 0;
        while (!(RNG->SR & RNG_SR_DRDY))
        {
            if (++to > 2000000u) return -1;
        }

        uint32_t word = RNG->DR;
        uint8_t *b = (uint8_t *)&word;
        for (int i = 0; i < 4 && produced < n; i++)
        {
            out[produced++] = b[i];
        }
    }

    g_rng_bytes += n;
    return 0;
}

/* ---------------------------------------------------------------------------
 * 小工具：手写十进制/十六进制格式化，避免依赖 libc 的 snprintf/atoi 链接。
 * ------------------------------------------------------------------------- */
static void u32_to_ascii(uint32_t v, char *out, int *len)
{
    char tmp[12];
    int ti = 0;
    if (v == 0u) tmp[ti++] = '0';
    else
    {
        while (v) { tmp[ti++] = (char)('0' + (v % 10u)); v /= 10u; }
    }
    while (ti > 0) out[(*len)++] = tmp[--ti];
}

static const char HEXC[16] = "0123456789ABCDEF";
static void byte_to_hex(uint8_t v, char *out, int *len)
{
    out[(*len)++] = HEXC[(v >> 4) & 0xF];
    out[(*len)++] = HEXC[v & 0xF];
}

/* ---------------------------------------------------------------------------
 * 在 main 循环轮询：收到一行 USART1 命令时处理并回送。
 * ------------------------------------------------------------------------- */
void rng_service_poll(void)
{
    if ((g_usart_rx_sta & 0x8000U) == 0U) return;   /* 无完整行 */

    uint16_t len = g_usart_rx_sta & 0x3FFFU;
    if (len >= USART_REC_LEN) len = (USART_REC_LEN - 1U);

    char line[USART_REC_LEN];
    for (uint16_t i = 0; i < len; i++)
    {
        line[i] = (char)g_usart_rx_buf[i];
    }
    line[len] = 0;

    g_usart_rx_sta = 0;   /* 消费：允许 ISR 接收下一行 */

    /* ---- $?RND,<n> ---- */
    if (len >= 6 && strncmp(line, "$?RND,", 6) == 0)
    {
        uint32_t n = 0;
        int i = 6;
        while (i < (int)len && line[i] >= '0' && line[i] <= '9')
        {
            n = n * 10u + (uint32_t)(line[i] - '0');
            i++;
        }
        if (n < 1u) n = 1u;
        if (n > RNG_MAX_REQ) n = RNG_MAX_REQ;

        if (rng_service_bytes(s_obuf, n) == 0)
        {
            char hdr[40];
            int hl = 0;
            hdr[hl++] = '$'; hdr[hl++] = '!'; hdr[hl++] = 'R';
            hdr[hl++] = 'N'; hdr[hl++] = 'D'; hdr[hl++] = ',';
            u32_to_ascii(n, hdr, &hl);

            uint8_t x = 0;
            for (int k = 0; k < hl; k++) x ^= (uint8_t)hdr[k];

            hdr[hl++] = '*';
            byte_to_hex(x, hdr, &hl);
            hdr[hl++] = '\r';
            hdr[hl++] = '\n';

            uart1_send((const uint8_t *)hdr, (uint32_t)hl);
            uart1_send(s_obuf, n);
        }
        else
        {
            uart1_send((const uint8_t *)"$!ERR\r\n", 7);
        }
    }
    /* ---- $?PING ---- */
    else if (len >= 6 && strncmp(line, "$?PING", 6) == 0)
    {
        uart1_send((const uint8_t *)"$!PONG\r\n", 8);
    }
    /* 未知命令：忽略（不回显，避免干扰主机解析） */
}
