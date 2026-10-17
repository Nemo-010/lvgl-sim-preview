# Copyright (c) 2026 Nemo
# SPDX-License-Identifier: Apache-2.0

if(CONFIG_LVGL_PREVIEW)

# Reported in the manifest and the frame header so a host watching several
# builds can tell them apart. APPLICATION_SOURCE_DIR is exported to Kconfig by
# Zephyr's own kconfig.cmake and points at the application directory, not at
# this module, so the default is taken from CMake instead of Kconfig.
set(LVGL_PREVIEW_APP_NAME "" CACHE STRING "Application name reported to the preview host")
if(NOT LVGL_PREVIEW_APP_NAME)
  get_filename_component(LVGL_PREVIEW_APP_NAME "${APPLICATION_SOURCE_DIR}" NAME)
endif()
zephyr_compile_definitions(CONFIG_LVGL_PREVIEW_APP_NAME="${LVGL_PREVIEW_APP_NAME}")

zephyr_library()

zephyr_library_include_directories(
  ${CMAKE_CURRENT_SOURCE_DIR}/../include
)

zephyr_library_sources(lvgl_preview.c)

if(CONFIG_LVGL_PREVIEW_INPUT)
  zephyr_library_sources(lvgl_preview_input.c)
endif()

# The transport lives in the native simulator runner context, next to Zephyr's
# own "bottom" drivers, so it is built with the host C library and host headers
# while the device side only sees opaque handles.
if(CONFIG_ARCH_POSIX)
  target_sources(native_simulator INTERFACE ${CMAKE_CURRENT_SOURCE_DIR}/lvgl_preview_bottom.c)
  target_compile_options(native_simulator INTERFACE
    "-I${CMAKE_CURRENT_SOURCE_DIR}"
    "-I${CMAKE_CURRENT_SOURCE_DIR}/../include")
endif()

endif()
