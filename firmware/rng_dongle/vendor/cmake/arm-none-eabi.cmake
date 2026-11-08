# ===========================================================================
# arm-none-eabi 交叉编译工具链文件（YMGUI → STM32H743）
#
# 用法：cmake -S . -B build -DCMAKE_TOOLCHAIN_FILE=cmake/arm-none-eabi.cmake -G Ninja
#
# 为什么用 arm-none-eabi-gcc 而不是 Keil：
#   YMGUI 是 C99 + **GNU 扩展**（语句表达式、typeof、__attribute__ 等），
#   Keil AC5/AC6 对 GNU 扩展不友好。设计说明 §3.3。
#
# 为什么用 Ninja 而不是 Make：
#   本机没有 `make`，但 IDE 自带的 Ninja 在 PATH 里。
# ===========================================================================

set(CMAKE_SYSTEM_NAME      Generic)
set(CMAKE_SYSTEM_PROCESSOR arm)

# 本机 ARM GNU Toolchain 装在含空格路径下，补进搜索路径；换机器时只需把
# arm-none-eabi-* 加入 PATH，下面两条找不到也不会阻塞 find_program。
list(APPEND CMAKE_PROGRAM_PATH "<本机路径> rel1/bin")
list(APPEND CMAKE_PROGRAM_PATH "<本机路径> rel1/bin")

set(TOOLCHAIN_PREFIX arm-none-eabi-)

find_program(CMAKE_C_COMPILER   NAMES ${TOOLCHAIN_PREFIX}gcc REQUIRED)
find_program(CMAKE_ASM_COMPILER NAMES ${TOOLCHAIN_PREFIX}gcc REQUIRED)
find_program(CMAKE_CXX_COMPILER NAMES ${TOOLCHAIN_PREFIX}g++ REQUIRED)
find_program(CMAKE_OBJCOPY      NAMES ${TOOLCHAIN_PREFIX}objcopy REQUIRED)
find_program(CMAKE_OBJDUMP      NAMES ${TOOLCHAIN_PREFIX}objdump REQUIRED)
find_program(CMAKE_SIZE         NAMES ${TOOLCHAIN_PREFIX}size REQUIRED)

# 交叉编译时不要尝试链接可执行文件来"探测编译器能力"——裸机没有默认启动文件，
# 链接必然失败，会让 CMake 误判工具链不可用。
set(CMAKE_TRY_COMPILE_TARGET_TYPE STATIC_LIBRARY)

set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)

# 目标：Cortex-M7，硬浮点（单精度，H7 的 FPv5-SP/D16）
set(MCU_FLAGS "-mcpu=cortex-m7 -mthumb -mfpu=fpv5-d16 -mfloat-abi=hard")
set(CMAKE_C_FLAGS_INIT   "${MCU_FLAGS} -ffunction-sections -fdata-sections")
set(CMAKE_ASM_FLAGS_INIT "${MCU_FLAGS} -x assembler-with-cpp")
# map 文件由 firmware/rng_dongle/CMakeLists.txt 用 target_link_options 指定到构建目录，
# 这里不再硬编码另一个工程的名字。
set(CMAKE_EXE_LINKER_FLAGS_INIT "${MCU_FLAGS} -Wl,--gc-sections")
