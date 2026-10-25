# SPDX-License-Identifier: Apache-2.0

get_property(_zro_runners GLOBAL PROPERTY ZEPHYR_RUNNERS)
if(NOT "openocd" IN_LIST _zro_runners)
  return()
endif()

# Python owns override validation and reads the unchanged pathname. Selection
# validation deliberately leaves remote-only settings to actual runner execution.
execute_process(
  COMMAND "${PYTHON_EXECUTABLE}" "${ZEPHYR_CURRENT_CMAKE_DIR}/config_default.py"
  RESULT_VARIABLE _zro_config_result
  OUTPUT_VARIABLE _zro_selection
  ERROR_VARIABLE _zro_config_error
  OUTPUT_STRIP_TRAILING_WHITESPACE
)
if(NOT _zro_config_result EQUAL 0)
  message(FATAL_ERROR "${_zro_config_error}")
endif()
string(JSON _zro_default GET "${_zro_selection}" runner)
string(JSON _zro_config GET "${_zro_selection}" path)

# Python rejects parent components, so dependencies can use the selected path
# directly without changing symlink traversal during generator normalization.
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${_zro_config}")

# Track creation and deletion without interpreting glob characters in the path.
set(_zro_config_glob "${_zro_config}")
string(REPLACE "[" "[[]" _zro_config_glob "${_zro_config_glob}")
string(REPLACE "*" "[*]" _zro_config_glob "${_zro_config_glob}")
string(REPLACE "?" "[?]" _zro_config_glob "${_zro_config_glob}")
file(GLOB _zro_config_presence
     CONFIGURE_DEPENDS
     LIST_DIRECTORIES false
     "${_zro_config_glob}")

string(MAKE_C_IDENTIFIER openocd _zro_openocd_id)
string(MAKE_C_IDENTIFIER remote_openocd _zro_remote_id)
get_property(_zro_openocd_args GLOBAL PROPERTY
             "BOARD_RUNNER_ARGS_${_zro_openocd_id}")
set_property(GLOBAL PROPERTY "BOARD_RUNNER_ARGS_${_zro_remote_id}"
             ${_zro_openocd_args})
set_property(GLOBAL APPEND PROPERTY ZEPHYR_RUNNERS remote_openocd)

if(_zro_default STREQUAL "remote_openocd")
  set(BOARD_FLASH_RUNNER remote_openocd PARENT_SCOPE)
  set(BOARD_DEBUG_RUNNER remote_openocd PARENT_SCOPE)
elseif(_zro_default STREQUAL "openocd")
  set(BOARD_FLASH_RUNNER openocd PARENT_SCOPE)
  set(BOARD_DEBUG_RUNNER openocd PARENT_SCOPE)
else()
  message(FATAL_ERROR "Unexpected Zephyr Remote OpenOCD default: ${_zro_default}")
endif()
