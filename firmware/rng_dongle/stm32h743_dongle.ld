/* ===========================================================================
 * STM32H743VIT6 链接脚本 —— RNG 最小固件专用（rng_dongle）
 *
 * 为什么不用主工程的 linker/stm32h743vit6.ld：
 *   · 主脚本里有一串 `EXTERN(...)` 专门给 GUI 固件的自定义 ISR 保活
 *     （TIM6_DAC / EXTI15_10 / DMA1_Stream4），本固件不定义它们 ⇒ 链接直接失败；
 *   · 主脚本还划了 QSPI/heap1/多块 SRAM 段与 IME/DMA/SD 专用 .bss 段，
 *     那套是给 YMGUI 用的，最小固件只需要 FLASH + DTCM。
 *
 * 本脚本刻意**只保留两段**：
 *   FLASH 2 MB（.isr_vector/.text/.rodata + .data 的加载镜像）
 *   DTCM 128 KB（.data/.bss/栈/堆）—— 0x2000_0000 起，是 H7 唯一可当普通 RAM 用的
 *   低地址段；0x2002_0000 之后是保留区，铺过去就 HardFault（RM0433 §2.3）。
 *
 * 符号名必须与 src/startup_gcc.s + newlib 对齐：_estack / _sidata / _sdata /
 * _edata / _sbss / _ebss / end。
 * =========================================================================== */

ENTRY(Reset_Handler)

MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 2048K
  DTCM  (rwx) : ORIGIN = 0x20000000, LENGTH = 128K
}

/* Cortex-M 满递减栈：栈顶 = DTCM 末端 */
_estack = ORIGIN(DTCM) + LENGTH(DTCM);

/* ⚠ 唯一需要保活的 ISR。startup_gcc.s 里 USART1_IRQHandler 是**强引用**（无 .weak），
 *   但函数本身在 -ffunction-sections 下仍可能被 --gc-sections 回收 ——
 *   显式 EXTERN 一次，行为与主工程那份脚本里的教训一致。 */
EXTERN(USART1_IRQHandler)

SECTIONS
{
  /* ---- 向量表：必须落在 flash 起始（硬件从 0x0800_0000 取 MSP/PC） ---- */
  .isr_vector :
  {
    . = ALIGN(4);
    KEEP(*(.isr_vector))
    . = ALIGN(4);
  } > FLASH

  /* ---- 代码与只读数据 ---- */
  .text :
  {
    . = ALIGN(4);
    *(.text)
    *(.text*)
    *(.glue_7)
    *(.glue_7t)
    *(.eh_frame)
    KEEP(*(.init))
    KEEP(*(.fini))
    . = ALIGN(4);
  } > FLASH

  .rodata :
  {
    . = ALIGN(4);
    *(.rodata)
    *(.rodata*)
    . = ALIGN(4);
  } > FLASH

  .ARM.extab : { *(.ARM.extab* .gnu.linkonce.armextab.*) } > FLASH
  .ARM : {
    __exidx_start = .;
    *(.ARM.exidx*)
    __exidx_end = .;
  } > FLASH

  /* .data 的初值存在 flash，启动时由 board_init_memory() 拷到 DTCM */
  _sidata = LOADADDR(.data);

  .data :
  {
    . = ALIGN(4);
    _sdata = .;
    *(.data)
    *(.data*)
    *(.RamFunc)
    *(.RamFunc*)
    . = ALIGN(4);
    _edata = .;
  } > DTCM AT> FLASH

  .bss (NOLOAD) :
  {
    . = ALIGN(4);
    _sbss = .;
    __bss_start__ = _sbss;
    *(.bss)
    *(.bss*)
    *(COMMON)
    . = ALIGN(4);
    _ebss = .;
    __bss_end__ = _ebss;
  } > DTCM

  /* newlib 的 _sbrk 会引用 `end`；本固件不用 malloc，但保留该符号免得链接报错 */
  PROVIDE(end = .);
  PROVIDE(_end = .);
}
