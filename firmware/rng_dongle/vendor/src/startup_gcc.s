/* ===========================================================================
 * GCC 版启动代码 + 向量表（STM32H743VIT6）
 *
 * 为什么自己写：CMSIS 资料包里只有 Keil(arm) 版 startup_stm32h743xx.s，
 * 它用 ARMCC 语法（PROC/ENDP/IMPORT），且 Reset_Handler 调 Keil C 库入口
 * `__main` —— 在 GNU 工具链下链接不过。本文件按 GNU as 语法重写。
 *
 * 中断都先指向 Default_Handler（死循环）。YMGUI 是轮询式、无中断驱动，
 * 需要某个中断时在 C 里定义同名函数即可覆盖（都是 weak 符号）。
 * =========================================================================== */

.syntax unified
.cpu cortex-m7
.fpu fpv5-d16
.thumb

.global __Vectors
.global Reset_Handler
.global Default_Handler

/* ====================== 向量表 ====================== */
.section .isr_vector, "a", %progbits
.type __Vectors, %object
__Vectors:
    .word  _estack                  /* 初始 MSP（链接脚本给：DTCM 末端） */
    .word  Reset_Handler
    /* ---- Cortex-M7 内核异常 ---- */
    .word  NMI_Handler
    .word  HardFault_Handler
    .word  MemManage_Handler
    .word  BusFault_Handler
    .word  UsageFault_Handler
    .word  0
    .word  0
    .word  0
    .word  0
    .word  SVC_Handler
    .word  DebugMon_Handler
    .word  0
    .word  PendSV_Handler
    .word  SysTick_Handler
    /* ---- 外部中断：STM32H743 有 150 个 IRQ（IRQ0..IRQ149）----
     * 原来是 `.rept 150 / .word Default_Handler`，所有外设中断都落进死循环。
     * （L2-4）为 SDMMC1 单独开槽：IDMA 传输完成必须靠中断回调
     * HAL_SD_IRQHandler，否则 HAL 的状态机不会收尾（hsd->State 永远停在 BUSY）。
     * 为 USART6 开槽：ESP32 上行链路（NTP/天气）靠 RXNE 中断
     * 收字节，落 Default_Handler 会直接死循环。USART6_IRQn = 71。
     * 为 JPEG 开槽：硬件 JPEG 解码的输出 FIFO 必须靠中断搬运，
     *   HAL_JPEG_Decode(polling) 实测**超时**（诊断值=3）且输出缓冲没拿到数据，
     *   所以改用 HAL_JPEG_Decode_IT。JPEG_IRQn = 121。
     * ：上行链路从 USART6(PC6/PC7) 换到 **UART4(PB9/PB8)** ——
     *   PC6 那根命令线始终不通（双向发波验证过与对端无电气连接），而 USART6 在
     *   LQFP100 上只有 PC6/PC7 一组，⇒ 换脚只能换外设。UART4_IRQn = 52。
     *   USART6 槽位保留（已停用，弱别名兜底）。
     * 为 TIM6_DAC 开槽：音频出声用 TIM6 周期中断在 ISR 里滴灌 I2S TX FIFO
     *   （主循环一帧 ~16ms 远慢于 FIFO 82µs 预算，必须靠高频中断）。TIM6_DAC_IRQn = 54。
     *   若落 Default_Handler 会死循环冻结内核。强定义在 src/audio_out.c，弱别名兜底。
     * 为 EXTI15_10 开槽：触摸 INT=PB13 走 EXTI 边沿中断做"有触摸才读 I2C"
     *   的门控（见 src/touch_port.c）。EXTI15_10_IRQn = 40。强定义在 src/touch_port.c，弱别名兜底。
     * 为 DMA1_Stream4 开槽：I2S2 TX 改走 DMA（半/全传中断补数），
     *   DMA1_Stream4_IRQn = 15。强定义在 src/audio_out.c，弱别名兜底。
     * 拆成 15 + 1 + 21 + 1 + 2 + 1 + 8 + 1 + 2 + 1 + 1 + 1 + 16 + 1 + 49 + 1 + 28 = 150，顺序不能错：位置由 CMSIS 的 IRQn 决定。
     * 为 USART1 开槽：RNG 命令接收需要 RX 中断（见 rng_service.c）。
     *   USART1_IRQn = 37，原先落在 IRQ16..IRQ39 的 Default_Handler 里，一收到字节就死循环冻结内核。
     *   现拆 IRQ16..IRQ36(21) + IRQ37(USART1) + IRQ38..IRQ39(2)。 */
    .rept  15                           /* IRQ0..IRQ14 */
    .word  Default_Handler
    .endr
    .word  DMA1_Stream4_IRQHandler      /* IRQ15 = DMA1_Stream4（I2S2 TX DMA，强定义在 src/audio_out.c） */
    .rept  21                           /* IRQ16..IRQ36 */
    .word  Default_Handler
    .endr
    .word  USART1_IRQHandler            /* IRQ37 = USART1（RNG 命令 $?PING/$?RND 的接收中断，
                                         *   强定义在 third_party/SYSTEM/usart/usart.c 的 USART_UX_IRQHandler；
                                         *   不在此开槽会落 Default_Handler 死循环，导致一收到字节就冻结内核） */
    .rept  2                            /* IRQ38..IRQ39 */
    .word  Default_Handler
    .endr
    .word  EXTI15_10_IRQHandler         /* IRQ40 = EXTI15_10（触摸 INT=PB13，强定义在 src/touch_port.c） */
    .rept  8                            /* IRQ41..IRQ48 */
    .word  Default_Handler
    .endr
    .word  SDMMC1_IRQHandler            /* IRQ49 = SDMMC1（见 sd_card.c） */
    .rept  2                            /* IRQ50..IRQ51 */
    .word  Default_Handler
    .endr
    .word  UART4_IRQHandler             /* IRQ52 = UART4（上行链路，见 uart_link.c） */
    .word  Default_Handler              /* IRQ53 = UART5 */
    .word  TIM6_DAC_IRQHandler          /* IRQ54 = TIM6/DAC（音频滴灌，强定义在 src/audio_out.c） */
    .rept  16                           /* IRQ55..IRQ70 */
    .word  Default_Handler
    .endr
    .word  USART6_IRQHandler            /* IRQ71 = USART6（已改 UART4，槽位保留备用） */
    .rept  49                           /* IRQ72..IRQ120 */
    .word  Default_Handler
    .endr
    .word  JPEG_IRQHandler              /* IRQ121 = JPEG（见 img_store.c 的 import_jpeg） */
    .rept  28                           /* IRQ122..IRQ149 */
    .word  Default_Handler
    .endr
.size __Vectors, . - __Vectors

/* ====================== 复位入口 ====================== */
.section .text.Reset_Handler, "ax", %progbits
.type Reset_Handler, %function
Reset_Handler:
    /* 1) 设栈指针（链接脚本的 _estack = DTCM 末端） */
    ldr   sp, =_estack

    /* 2) 使能 FPU：CPACR 的 CP10/CP11 全访问。
     *    Cortex-M7 复位后 FPU 关闭，此时任何浮点指令都会触发 UsageFault。
     *    应用层要用 float（设计说明允许），所以必须在这里打开。 */
    ldr   r0, =0xE000ED88           /* SCB->CPACR */
    ldr   r1, [r0]
    orr   r1, r1, #(0xF << 20)      /* CP10=11, CP11=11 */
    str   r1, [r0]
    dsb
    isb

    /* 3) CMSIS 系统初始化（向量表偏移、SystemCoreClock 更新） */
    bl    SystemInit

    /* 4) 把 .data 从 flash 搬到 DTCM、清 .bss（板级 C 实现，见 board_startup.c） */
    bl    board_init_memory

    /* 5) C 库初始化（__attribute__((constructor)) 等） */
    bl    __libc_init_array

    /* 6) 进 main，不应返回 */
    bl    main

1:  b     1b
.size Reset_Handler, . - Reset_Handler

/* ====================== 默认中断处理 ====================== */
.section .text.Default_Handler, "ax", %progbits
.type Default_Handler, %function
Default_Handler:
    b     .
.size Default_Handler, . - Default_Handler

/* 把所有内核异常与 IRQ 弱绑定到 Default_Handler。
 * `.thumb_set` 让它们拥有正确的 bit0=1（Thumb 状态）。 */
.weak NMI_Handler
.thumb_set NMI_Handler, Default_Handler
.weak HardFault_Handler
.thumb_set HardFault_Handler, Default_Handler
.weak MemManage_Handler
.thumb_set MemManage_Handler, Default_Handler
.weak BusFault_Handler
.thumb_set BusFault_Handler, Default_Handler
.weak UsageFault_Handler
.thumb_set UsageFault_Handler, Default_Handler
.weak SVC_Handler
.thumb_set SVC_Handler, Default_Handler
.weak DebugMon_Handler
.thumb_set DebugMon_Handler, Default_Handler
.weak PendSV_Handler
.thumb_set PendSV_Handler, Default_Handler
.weak SysTick_Handler
.thumb_set SysTick_Handler, Default_Handler

/* SDMMC1（IRQ49）：强定义在 src/sd_card.c，这里给弱别名做兜底
 * —— 万一 sd_card.c 没编进来，也不会链接失败，只会落回死循环（行为同其他未用 IRQ）。 */
.weak SDMMC1_IRQHandler
.thumb_set SDMMC1_IRQHandler, Default_Handler

/* UART4（IRQ52）：强定义在 src/uart_link.c，同样给弱别名兜底。 */
.weak UART4_IRQHandler
.thumb_set UART4_IRQHandler, Default_Handler

/* USART6（IRQ71）：上行链路已改用 UART4，此槽位保留备用（弱别名兜底）。 */
.weak USART6_IRQHandler
.thumb_set USART6_IRQHandler, Default_Handler

/* TIM6_DAC（IRQ54）：音频滴灌用的高频定时中断，强定义在 src/audio_out.c。
 * 这里给弱别名兜底 —— 万一 audio_out.c 没编进来，也不会链接失败，只会落回死循环。 */
.weak TIM6_DAC_IRQHandler
.thumb_set TIM6_DAC_IRQHandler, Default_Handler

/* EXTI15_10（IRQ40）：触摸 INT=PB13 的边沿中断，强定义在 src/touch_port.c。
 * 弱别名兜底 —— 万一 touch_port.c 没编进来，也不会链接失败，只会落回死循环。 */
.weak EXTI15_10_IRQHandler
.thumb_set EXTI15_10_IRQHandler, Default_Handler

/* DMA1_Stream4（IRQ15）：I2S2 TX 的 DMA 半/全传中断，强定义在 src/audio_out.c。
 * 弱别名兜底 —— 万一 audio_out.c 没编进来，也不会链接失败，只会落回死循环。 */
.weak DMA1_Stream4_IRQHandler
.thumb_set DMA1_Stream4_IRQHandler, Default_Handler
