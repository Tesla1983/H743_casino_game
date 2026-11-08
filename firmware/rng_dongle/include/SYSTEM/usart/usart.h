/**
 * @file        usart.h   （rng_dongle 专用垫片）
 * @brief       与第三方 `SYSTEM/usart/usart.h` **同名同符号**的极简替代
 *
 * 为什么需要它：
 *   src/rng_service.c 里写的是 `#include "./SYSTEM/usart/usart.h"`，只用到 4 个符号
 *   （USART_UX / USART_REC_LEN / g_usart_rx_buf / g_usart_rx_sta）。最小固件不该为此
 *   把 vendor 的 usart.c（带 printf 重定向、HAL 句柄、200 B 环形缓冲）整份拖进来，
 *   所以在本目录放一份同名头，靠 -I 顺序（本目录在前）把 vendor 的那份顶掉。
 *
 * 语义与 vendor 完全一致（rng_service.c 直接依赖这两点，改不得）：
 *   g_usart_rx_sta  bit15 = 收到完整一行；bit13..0 = 该行长度
 *   g_usart_rx_buf  行内容（不含结束符）
 *
 * 与 vendor 的唯一差别：这里把两个全局声明为 volatile —— 它们由 USART1 中断写、
 * 主循环读，vendor 版没写 volatile 属于历史遗留（能跑但理论上会被优化掉读缓存）。
 */
#ifndef __DONGLE_USART_H
#define __DONGLE_USART_H

#include <stdint.h>

/* USART1：PA9=TX / PA10=RX（与本板 CH340 的接线一致，见 内部文档） */
#define USART_UX                USART1
#define USART_UX_IRQn           USART1_IRQn
#define USART_UX_IRQHandler     USART1_IRQHandler

#define USART_REC_LEN           200u    /* 与 vendor 相同：单行命令上限 200 B */

extern volatile uint8_t  g_usart_rx_buf[USART_REC_LEN];
extern volatile uint16_t g_usart_rx_sta;

#endif /* __DONGLE_USART_H */
