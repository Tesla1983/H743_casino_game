# ===========================================================================
# RNG 最小固件（rng_dongle）—— 老虎机工程的**随包发布副本**
#
# 这份目录复制自 E:/ymgui-h743/firmware/rng_dongle（2026-11-02），目的是让老虎机工程
# 自己就能交付/烧录固件，不必先去翻另一个仓库。
#
# ⚠ 单一真源约定：**共享代码不复制**。
#   `rng_service.c`（协议 / 头帧 XOR / SECS-CECS 恢复）、`startup_gcc.s`、`third_party/`
#   （CMSIS + HAL 头）仍然只在 ymgui-h743 维护一份，本工程用 -DYMGUI_ROOT=<路径> 指过去。
#   这里只放 dongle 专属文件（main_dongle.c / 垫片头 / 链接脚本 / 本文件）。
#   两边同步校验见 firmware/README.md。
#
# 构建（在老虎机仓库根目录）：
#   cmake -S firmware/rng_dongle -B build-dongle -G Ninja \
#         -DCMAKE_MAKE_PROGRAM="<ninja 路径>" \
#         -DCMAKE_TOOLCHAIN_FILE="E:/ymgui-h743/cmake/arm-none-eabi.cmake" \
#         -DYMGUI_ROOT="E:/ymgui-h743"
#   cmake --build build-dongle
#
# 不想构建？直接用 firmware/prebuilt/rng_dongle-1.0.bin 烧录（见 README）。
# ===========================================================================

cmake_minimum_required(VERSION 3.20)
project(rng_dongle C ASM)

# ymgui-h743 仓库根：提供 rng_service.c / startup_gcc.s / CMSIS+HAL 头
set(YMGUI_ROOT "E:/ymgui-h743" CACHE PATH "ymgui-h743 仓库根（共享源码的单一真源）")

foreach(need
        "${YMGUI_ROOT}/src/rng_service.c"
        "${YMGUI_ROOT}/src/rng_service.h"
        "${YMGUI_ROOT}/src/startup_gcc.s"
        "${YMGUI_ROOT}/third_party/CMSIS/Device/ST/STM32H7xx/Include/stm32h7xx.h")
    if(NOT EXISTS "${need}")
        message(FATAL_ERROR
            "找不到共享源码：${need}\n"
            "请用 -DYMGUI_ROOT=<ymgui-h743 路径> 指向正确仓库根；\n"
            "或跳过构建，直接烧 firmware/prebuilt/rng_dongle-1.0.bin。")
    endif()
endforeach()

set(ROOT "${YMGUI_ROOT}")
set(TP "${ROOT}/third_party")

add_compile_options(-O2 -Wall -Wno-unused-parameter)
add_compile_definitions(USE_HAL_DRIVER STM32H743xx)

include_directories(
	${CMAKE_CURRENT_SOURCE_DIR}/include      # 垫片优先：SYSTEM/usart/usart.h
	${ROOT}/src                              # rng_service.h / stm32h7xx_hal_conf.h
	${TP}/CMSIS/Device/ST/STM32H7xx/Include
	${TP}/CMSIS/Include
	${TP}/STM32H7xx_HAL_Driver/Inc
)

add_executable(rng_dongle.elf
	src/main_dongle.c
	${ROOT}/src/rng_service.c
	${ROOT}/src/startup_gcc.s
	${TP}/CMSIS/Device/ST/STM32H7xx/Source/Templates/system_stm32h7xx.c
)

set(DONGLE_LD "${CMAKE_CURRENT_SOURCE_DIR}/stm32h743_dongle.ld")

target_link_options(rng_dongle.elf PRIVATE
	-T${DONGLE_LD}
	-Wl,--gc-sections
	-Wl,-Map=${CMAKE_CURRENT_BINARY_DIR}/rng_dongle.map
	-Wl,--print-memory-usage
	--specs=nano.specs
	--specs=nosys.specs
)

# 改链接脚本必须触发重链（与主工程同一个坑，见 ymgui-h743 根 CMakeLists 注释）
set_target_properties(rng_dongle.elf PROPERTIES LINK_DEPENDS ${DONGLE_LD})

add_custom_command(TARGET rng_dongle.elf POST_BUILD
	COMMAND ${CMAKE_OBJCOPY} -O binary $<TARGET_FILE:rng_dongle.elf>
	        ${CMAKE_CURRENT_BINARY_DIR}/rng_dongle.bin
	COMMAND ${CMAKE_SIZE} $<TARGET_FILE:rng_dongle.elf>
	COMMENT "生成 rng_dongle.bin"
)
