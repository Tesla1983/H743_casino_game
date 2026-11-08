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
     * 默认全是 `.word Default_Handler`；需要独立 ISR 的外设按 IRQn 开槽：
     *   IRQ15  = DMA1_Stream4（I2S2 TX DMA）
     *   IRQ37  = USART1（RNG 命令接收）
     *   IRQ40  = EXTI15_10（触摸中断）
     *   IRQ54  = TIM6_DAC（音频 FIFO 滴灌）
     *   IRQ71  = USART6（备用上行链路，已停用）
     *   IRQ121 = JPEG（解码输出 FIFO 搬运）
     * 槽位顺序由 CMSIS 的 IRQn 决定，拆分为
     * 15 + 1 + 21 + 1 + 2 + 1 + 8 + 1 + 2 + 1 + 1 + 1 + 16 + 1 + 49 + 1 + 28 = 150，不能错位。
     * 其中 IRQ16..IRQ36(21) + IRQ37(USART1) + IRQ38..IRQ39(2) 需单独分段。 */
    .rept  15                           /* IRQ0..IRQ14 */
    .word  Default_Handler
    .endr
    .word  DMA1_Stream4_IRQHandler      /* IRQ15 = DMA1_Stream4（I2S2 TX DMA，由应用层提供强定义） */
    .rept  21                           /* IRQ16..IRQ36 */
    .word  Default_Handler
    .endr
    .word  USART1_IRQHandler            /* IRQ37 = USART1（RNG 命令 $?PING/$?RND 的接收中断，
                                         *   USART_UX_IRQHandler 由应用层提供强定义；
                                         *   不在此开槽会落 Default_Handler 死循环，导致一收到字节就冻结内核） */
    .rept  2                            /* IRQ38..IRQ39 */
    .word  Default_Handler
    .endr
    .word  EXTI15_10_IRQHandler         /* IRQ40 = EXTI15_10（触摸中断，由应用层提供强定义） */
    .rept  8                            /* IRQ41..IRQ48 */
    .word  Default_Handler
    .endr
    .word  SDMMC1_IRQHandler            /* IRQ49 = SDMMC1（SD 卡驱动） */
    .rept  2                            /* IRQ50..IRQ51 */
    .word  Default_Handler
    .endr
    .word  UART4_IRQHandler             /* IRQ52 = UART4（上行链路） */
    .word  Default_Handler              /* IRQ53 = UART5 */
    .word  TIM6_DAC_IRQHandler          /* IRQ54 = TIM6/DAC（音频滴灌，由应用层提供强定义） */
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
     *    应用层使用硬件浮点，所以必须在这里打开。 */
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

/* SDMMC1（IRQ49）：由应用层提供强定义，这里给弱别名做兜底
 * —— 万一强定义没编进来，也不会链接失败，只会落回死循环（行为同其他未用 IRQ）。 */
.weak SDMMC1_IRQHandler
.thumb_set SDMMC1_IRQHandler, Default_Handler

/* UART4（IRQ52）：由应用层提供强定义，同样给弱别名兜底。 */
.weak UART4_IRQHandler
.thumb_set UART4_IRQHandler, Default_Handler

/* USART6（IRQ71）：备用上行链路槽位（弱别名兜底）。 */
.weak USART6_IRQHandler
.thumb_set USART6_IRQHandler, Default_Handler

/* TIM6_DAC（IRQ54）：音频滴灌用的高频定时中断，由应用层提供强定义。
 * 这里给弱别名兜底 —— 万一强定义没编进来，也不会链接失败，只会落回死循环。 */
.weak TIM6_DAC_IRQHandler
.thumb_set TIM6_DAC_IRQHandler, Default_Handler

/* EXTI15_10（IRQ40）：触摸的边沿中断，由应用层提供强定义。
 * 弱别名兜底 —— 万一强定义没编进来，也不会链接失败，只会落回死循环。 */
.weak EXTI15_10_IRQHandler
.thumb_set EXTI15_10_IRQHandler, Default_Handler

/* DMA1_Stream4（IRQ15）：I2S2 TX 的 DMA 半/全传中断，由应用层提供强定义。
 * 弱别名兜底 —— 万一强定义没编进来，也不会链接失败，只会落回死循环。 */
.weak DMA1_Stream4_IRQHandler
.thumb_set DMA1_Stream4_IRQHandler, Default_Handler
