# SPDX-License-Identifier: Apache-2.0

get_property(_zro_runners GLOBAL PROPERTY ZEPHYR_RUNNERS)
if(NOT "openocd" IN_LIST _zro_runners)
  return()
endif()

set(_zro_config "$ENV{HOME}/.config/zephyr-remote-openocd/config.toml")
if(DEFINED ENV{ZEPHYR_REMOTE_OPENOCD_CONFIG})
  set(_zro_config "$ENV{ZEPHYR_REMOTE_OPENOCD_CONFIG}")
endif()

# A normal incremental build asks CMake to reconfigure when this file changes.
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${_zro_config}")

string(MAKE_C_IDENTIFIER openocd _zro_openocd_id)
string(MAKE_C_IDENTIFIER remote-openocd _zro_remote_id)
get_property(_zro_openocd_args GLOBAL PROPERTY
             "BOARD_RUNNER_ARGS_${_zro_openocd_id}")
set_property(GLOBAL PROPERTY "BOARD_RUNNER_ARGS_${_zro_remote_id}"
             ${_zro_openocd_args})
set_property(GLOBAL APPEND PROPERTY ZEPHYR_RUNNERS remote-openocd)

execute_process(
  COMMAND "${PYTHON_EXECUTABLE}"
          "${ZEPHYR_CURRENT_MODULE_DIR}/scripts/config_default.py"
          "${_zro_config}"
  RESULT_VARIABLE _zro_config_result
  OUTPUT_VARIABLE _zro_default
  ERROR_VARIABLE _zro_config_error
  OUTPUT_STRIP_TRAILING_WHITESPACE
)
if(NOT _zro_config_result EQUAL 0)
  message(FATAL_ERROR "${_zro_config_error}")
endif()

if(_zro_default STREQUAL "remote")
  set(BOARD_FLASH_RUNNER remote-openocd PARENT_SCOPE)
  set(BOARD_DEBUG_RUNNER remote-openocd PARENT_SCOPE)
elseif(_zro_default STREQUAL "local")
  set(BOARD_FLASH_RUNNER openocd PARENT_SCOPE)
  set(BOARD_DEBUG_RUNNER openocd PARENT_SCOPE)
else()
  message(FATAL_ERROR "Unexpected Zephyr Remote OpenOCD default: ${_zro_default}")
endif()
