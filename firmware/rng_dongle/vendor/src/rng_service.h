/**
 * @file        rng_service.h
 * @brief       STM32H743 硬件 RNG 经 USART1 对外提供真随机源
 *
 * 设计要点：
 *   - H743 自带真随机源 RNG 外设（模拟噪声 + 健康检测），非 PRNG。
 *   - 时钟选 HSI48(48MHz) 作 RNGxCLK（H7 RNG 官方推荐时钟，远高于 CECS 阈值）。
 *   - 命令轮询放在 main 循环（拉取式，无中断竞争），命令分发只认 USART1 行式缓冲。
 *   - 协议见 rng_service.c 顶部注释；主机侧由随机源桥服务实现。
 *
 * 注意：本模块不改动标准 usart.c / printf 调试通道，
 *       只在 g_usart_rx_sta 标记"收到一行"时处理，处理完立即清零。
 */
#ifndef __RNG_SERVICE_H
#define __RNG_SERVICE_H

#include <stdint.h>
#include <stddef.h>

/* 单次请求可拉取的最大字节数（保护静态缓冲与串口吞吐）。 */
#define RNG_MAX_REQ  4096u

/* 初始化 RNG 外设与时钟。必须在 HAL/clocks 起来之后调用（main 里 usart_init 之后即可）。 */
void rng_service_init(void);

/* 填充 out[0..n) 为硬件真随机字节。返回 0=成功，-1=出现 RNG 错误（已尝试恢复）。 */
int  rng_service_bytes(uint8_t *out, uint32_t n);

/* 在 main 循环里轮询 USART1 命令（$?RND,<n> / $?PING），并回送响应。 */
void rng_service_poll(void);

/* 诊断量：SWD 可直接读取（验收用）。 */
extern uint64_t g_rng_bytes;   /* 累计产出字节数 */
extern uint32_t g_rng_err;     /* 累计 RNG 错误（SECS/CECS）次数 */

#endif /* __RNG_SERVICE_H */
