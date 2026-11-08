# ===========================================================================
# RNG 最小固件（rng_dongle）—— 本仓库可独立重建
#
# 目录内容：
#   src/main_dongle.c、include/SYSTEM/usart/usart.h、stm32h743_dongle.ld、本文件
#     = dongle 专属源码（从本仓库 2336f56^ 恢复的原件）
#   vendor/ = 构建所需的共享源码（rng_service.c/.h、startup_gcc.s、
#     stm32h7xx_hal_conf.h）与 CMSIS/HAL 头，按 Ninja 依赖清单从 共享源码工程
#     精确归档 99 个文件（约 6.5 MB），另含 cmake/arm-none-eabi.cmake。
#     克隆本仓库即可重建，不再依赖任何外部工程目录。
#
# 默认 YMGUI_ROOT 指向本目录 vendor/；若要改用 共享源码工程 的实时源码，
# 显式传 -DYMGUI_ROOT=<该工程根> 覆盖即可。
#
# 构建（在本仓库根目录，零额外参数）：
#   cmake -S firmware/rng_dongle -B build-dongle -G Ninja
#   cmake --build build-dongle
#   （工具链文件由本文件在 project() 前自动指定；CMake 会在 PATH 中找 ninja，
#    找不到时补 -DCMAKE_MAKE_PROGRAM=<ninja 路径>）
#
# 产物应与 firmware/prebuilt/rng_dongle-1.0.{bin,elf} 的 SHA256 一致
# （见 firmware/prebuilt/SHA256SUMS.txt）；不想构建可直接烧录预编译镜像。
# ===========================================================================

cmake_minimum_required(VERSION 3.20)

# 未显式指定时，自动使用本仓库 vendor/ 内的交叉编译工具链文件（必须在 project() 之前）。
# 显式传 -DCMAKE_TOOLCHAIN_FILE=... 仍可覆盖。
if(NOT DEFINED CMAKE_TOOLCHAIN_FILE)
    set(CMAKE_TOOLCHAIN_FILE "${CMAKE_CURRENT_SOURCE_DIR}/vendor/cmake/arm-none-eabi.cmake"
        CACHE FILEPATH "ARM 交叉编译工具链文件（默认本仓库 vendor/）")
endif()

project(rng_dongle C ASM)

# 共享源码根：默认本仓库 vendor/，可用 -DYMGUI_ROOT=<共享源码工程 根> 覆盖
set(YMGUI_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/vendor" CACHE PATH "共享源码根（默认本仓库 vendor/）")

foreach(need
        "${YMGUI_ROOT}/src/rng_service.c"
        "${YMGUI_ROOT}/src/rng_service.h"
        "${YMGUI_ROOT}/src/startup_gcc.s"
        "${YMGUI_ROOT}/third_party/CMSIS/Device/ST/STM32H7xx/Include/stm32h7xx.h")
    if(NOT EXISTS "${need}")
        message(FATAL_ERROR
            "找不到共享源码：${need}\n"
            "请确认 firmware/rng_dongle/vendor/ 已随本仓库完整检出；\n"
            "或用 -DYMGUI_ROOT=<路径> 指向 共享源码工程 工程根；\n"
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

# 改链接脚本必须触发重链（与主工程同一个坑，见 共享源码工程 根 CMakeLists 注释）
set_target_properties(rng_dongle.elf PROPERTIES LINK_DEPENDS ${DONGLE_LD})

add_custom_command(TARGET rng_dongle.elf POST_BUILD
	COMMAND ${CMAKE_OBJCOPY} -O binary $<TARGET_FILE:rng_dongle.elf>
	        ${CMAKE_CURRENT_BINARY_DIR}/rng_dongle.bin
	COMMAND ${CMAKE_SIZE} $<TARGET_FILE:rng_dongle.elf>
	COMMENT "生成 rng_dongle.bin"
)
