/**
 * @file        main_dongle.c
 * @brief       STM32H743 RNG 最小固件（为游戏随机源提供独立的轻量实现）
 *
 * 目标：上电后**立刻**能在 USART1 上应答 `$?PING` / `$?RND,<n>`，
 *       不初始化屏、音频、TF 卡、QSPI 字库、ESP32 链路。
 *       完整 GUI 固件要达到同样状态需要等 QSPI 字库启动（约 25 s），
 *       这对"随机源"是不可接受的启动延迟。
 *
 * 复用了已验证的共享源码 `vendor/src/rng_service.c`（协议、SECS/CECS 恢复、HDR XOR 全不动），
 * 只替换"谁提供那一行命令"：
 *   GUI 固件：vendor 的 SYSTEM/usart/usart.c（HAL + printf 重定向 + 200 B 缓冲）
 *   本固件  ：本文件里的裸寄存器 USART1 驱动（无 HAL、无 printf、无 malloc）
 *
 * 时钟：**保持复位默认**（SYSCLK = HSI 64 MHz，APB2 不分频），不碰 PLL/HSE。
 *   理由：RNG 自己用 HSI48 作 RNGCLK（rng_service_init 里配），与系统时钟无关；
 *   少配一层时钟就少一处"板上晶振不起振 ⇒ 卡死"的风险（本板 HSE=25 MHz，
 *   一旦配置失败，GUI 固件也只是降频继续，本固件干脆不依赖它）。
 *
 * 协议（与 GUI 固件完全一致，另有两条只读诊断命令）：
 *   $?PING            -> $!PONG
 *   $?RND,<n>         -> $!RND,<n>*HH\r\n + n 字节裸随机
 *   $?VER             -> $!VER,RNGDONGLE-1.0        （GUI 固件不答这条，可据此区分固件）
 *   $?STAT            -> $!STAT,<总字节>,<错误数>,<开机自检 0/1>
 */

#include <string.h>

#include "stm32h7xx_hal.h"            /* 只为 RCC/RNG/USART 的寄存器宏 */
#include "rng_service.h"
#include "./SYSTEM/usart/usart.h"     /* 本目录的垫片，语义同 vendor */

#define FW_TAG        "RNGDONGLE-1.0"
#define PCLK2_HZ      64000000u       /* 复位默认：HSI 64 MHz，APB2 分频 /1 */
#define UART_BAUD     115200u
#define RNG_BOOT_CHECK 32u            /* 开机取多少字节做"源卡死"粗检 */

/* rng_service.c 依赖的两个符号（语义见 include/SYSTEM/usart/usart.h） */
volatile uint8_t  g_usart_rx_buf[USART_REC_LEN];
volatile uint16_t g_usart_rx_sta = 0;

/* 诊断：1 = 开机粗检通过（32 字节不全相同），0 = 源疑似卡死 */
static uint8_t g_boot_ok = 0;

/* ------------------------------------------------------------------------- */
/* 启动：startup_gcc.s 里 `bl board_init_memory` —— 把 .data 从 flash 搬到 DTCM、清 .bss。
 * 完整 GUI 固件的 board_startup.c 还引用了 IME/DMA/SD 专用段符号，本固件不需要。 */
extern uint32_t _sidata, _sdata, _edata, _sbss, _ebss;

void board_init_memory(void)
{
    uint32_t *src = &_sidata;
    uint32_t *dst = &_sdata;
    while (dst < &_edata) { *dst++ = *src++; }
    for (dst = &_sbss; dst < &_ebss; ) { *dst++ = 0u; }
}

/* ------------------------------------------------------------------------- */
/* 裸寄存器 USART1：PA9=TX / PA10=RX，AF7，115200 8N1                          */
static void uart1_hw_init(void)
{
    /* GPIOA 时钟（AHB4）+ 等待生效：紧跟写入的寄存器访问在部分 H7 硅片上会丢 */
    RCC->AHB4ENR |= RCC_AHB4ENR_GPIOAEN;
    (void)RCC->AHB4ENR;

    /* PA9/PA10 → 复用推挽，AF7；RX 上拉（CH340 空闲时线路为高，上拉可避免悬空噪声） */
    GPIOA->MODER   = (GPIOA->MODER & ~(3u << (9u * 2u)) & ~(3u << (10u * 2u)))
                   | (2u << (9u * 2u)) | (2u << (10u * 2u));
    GPIOA->OTYPER &= ~(1u << 9);
    GPIOA->OSPEEDR |= (3u << (9u * 2u)) | (3u << (10u * 2u));
    GPIOA->PUPDR   = (GPIOA->PUPDR & ~(3u << (10u * 2u))) | (1u << (10u * 2u));
    /* AFR[1] 管 PA8..PA15：9→bit4..7、10→bit8..11 */
    GPIOA->AFR[1]  = (GPIOA->AFR[1]
                      & ~(0xFu << ((9u - 8u) * 4u))
                      & ~(0xFu << ((10u - 8u) * 4u)))
                   | (7u << ((9u - 8u) * 4u))
                   | (7u << ((10u - 8u) * 4u));

    RCC->APB2ENR |= RCC_APB2ENR_USART1EN;
    (void)RCC->APB2ENR;

    USART1->CR1 = 0u;                                   /* 先关，配置期间不动 */
    /* OVER8=0 时 BRR = fCK/baud（12.4 定点），556 ≙ 115.1 kbps，误差 0.08% */
    USART1->BRR = (PCLK2_HZ + (UART_BAUD / 2u)) / UART_BAUD;
    USART1->CR1 = USART_CR1_TE | USART_CR1_RE | USART_CR1_RXNEIE | USART_CR1_UE;

    NVIC_SetPriority(USART1_IRQn, 3u);
    NVIC_EnableIRQ(USART1_IRQn);
}

/* ------------------------------------------------------------------------- */
/* 行式接收：与 vendor 语义一致（bit15=完整行，低 13 位=长度），但终止符同时接受
 * '\r' 与 '\n'——"\r\n" 时在 '\r' 就成行，'\n' 到达时 sta 已清零、长度 0 直接忽略。 */
void USART1_IRQHandler(void)
{
    uint32_t isr = USART1->ISR;

    /* RXNE 与 ORE 都要读 DR：读 DR 会同时清 RXNE 与 ORE（RM0433 §47.8.8） */
    if ((isr & (USART_ISR_RXNE_RXFNE | USART_ISR_ORE)) != 0u)
    {
        uint8_t r = (uint8_t)(USART1->RDR & 0xFFu);

        if ((g_usart_rx_sta & 0x8000u) == 0u)          /* 上一行已消费才收新的 */
        {
            if (r == '\r' || r == '\n')
            {
                if ((g_usart_rx_sta & 0x3FFFu) != 0u)
                {
                    g_usart_rx_sta |= 0x8000u;         /* 标记成行 */
                }
            }
            else
            {
                uint16_t len = (uint16_t)(g_usart_rx_sta & 0x3FFFu);
                if (len < (USART_REC_LEN - 1u))
                {
                    g_usart_rx_buf[len] = r;
                    g_usart_rx_sta = (uint16_t)(len + 1u);
                }
                /* 超长：丢弃多余字节（保持行内前 199 B），不置错误位——协议层会拒 */
            }
        }
    }

    /* 噪声/帧错误：清掉，避免 ISR 反复触发（本链路只有 CH340，正常不会出现） */
    if ((isr & (USART_ISR_FE | USART_ISR_NE)) != 0u)
    {
        USART1->ICR = USART_ICR_FECF | USART_ICR_NECF;
    }
}

/* startup_gcc.s 的向量表里 JPEG_IRQHandler 是**强引用**（GUI 固件在 src/ 别处定义，
 * 因为那块板要用硬件 JPEG 解码）。本固件不下图像，给一个空实现把链接补上即可；
 * 同理若以后向量表又加了强引用项而本固件不定义，链接会明确报符号名，不会静默跑飞。 */
void JPEG_IRQHandler(void) { }

/* ------------------------------------------------------------------------- */
static void uart1_send(const uint8_t *p, uint32_t len)
{
    for (uint32_t i = 0; i < len; i++)
    {
        while ((USART1->ISR & USART_ISR_TXE_TXFNF) == 0u) { /* 等 TXE */ }
        USART1->TDR = p[i];
    }
    while ((USART1->ISR & USART_ISR_TC) == 0u) { /* 等最后一字节移出，防止立刻复位/关时钟丢尾字节 */ }
}

static void uart1_send_str(const char *s)
{
    uart1_send((const uint8_t *)s, (uint32_t)strlen(s));
}

/* 不依赖 printf：把 u32 追加成十进制 */
static int u32_ascii(uint32_t v, char *out, int at)
{
    char tmp[11];
    int n = 0;
    if (v == 0u) { tmp[n++] = '0'; }
    else { while (v) { tmp[n++] = (char)('0' + (v % 10u)); v /= 10u; } }
    while (n > 0) { out[at++] = tmp[--n]; }
    return at;
}

/* 把当前行**只读**拷出来（不消费）；返回长度，无完整行返回 -1 */
static int peek_line(char *out, uint16_t cap)
{
    if ((g_usart_rx_sta & 0x8000u) == 0u) return -1;

    uint16_t len = (uint16_t)(g_usart_rx_sta & 0x3FFFu);
    if (len >= cap) len = (uint16_t)(cap - 1u);

    for (uint16_t i = 0; i < len; i++) out[i] = (char)g_usart_rx_buf[i];
    out[len] = '\0';
    return (int)len;
}

static void send_stat(void)
{
    char buf[64];
    int at = 0;
    const char *head = "$!STAT,";
    while (*head) buf[at++] = *head++;
    at = u32_ascii((uint32_t)g_rng_bytes, buf, at);
    buf[at++] = ',';
    at = u32_ascii(g_rng_err, buf, at);
    buf[at++] = ',';
    at = u32_ascii((uint32_t)g_boot_ok, buf, at);
    buf[at++] = '\r';
    buf[at++] = '\n';
    uart1_send((const uint8_t *)buf, (uint32_t)at);
}

/* ------------------------------------------------------------------------- */
/* 开机粗检：连续 32 字节全相同 ⇒ 源疑似卡死（真随机出现这种情况的概率约 2^-248）。
 * 只置标志、不阻塞；详情由 $?STAT 报出来，避免"看着像是好的"这种默认假设。 */
static void rng_boot_check(void)
{
    uint8_t buf[RNG_BOOT_CHECK];
    if (rng_service_bytes(buf, RNG_BOOT_CHECK) != 0) { g_boot_ok = 0; return; }

    uint8_t same = 1;
    for (uint32_t i = 1; i < RNG_BOOT_CHECK; i++)
    {
        if (buf[i] != buf[0]) { same = 0; break; }
    }
    g_boot_ok = same ? 0u : 1u;
}

int main(void)
{
    uart1_hw_init();
    rng_service_init();          /* RNG 时钟 = HSI48，丢前 4 字（H7 勘误） */
    rng_boot_check();

    for (;;)
    {
        char line[USART_REC_LEN];
        int len = peek_line(line, (uint16_t)sizeof line);

        if (len < 0) continue;

        /* 只在最小固件里提供的两条只读命令；其余一律交给 rng_service 处理，
         * 这样协议主体（$?PING / $?RND）与 GUI 固件逐字节一致。 */
        if (len >= 5 && strncmp(line, "$?VER", 5) == 0)
        {
            g_usart_rx_sta = 0;
            uart1_send_str("$!VER," FW_TAG "\r\n");
        }
        else if (len >= 6 && strncmp(line, "$?STAT", 6) == 0)
        {
            g_usart_rx_sta = 0;
            send_stat();
        }
        else
        {
            rng_service_poll();   /* 内部会消费该行（清 g_usart_rx_sta） */
        }
    }
}
