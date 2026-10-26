# SPDX-License-Identifier: Apache-2.0

get_property(_zro_runners GLOBAL PROPERTY ZEPHYR_RUNNERS)
if(NOT "openocd" IN_LIST _zro_runners)
  return()
endif()

# Python owns override validation and reads the unchanged pathname. Selection
# validation deliberately leaves remote-only settings to actual runner execution.
set(_zro_config_selection "${CMAKE_CURRENT_BINARY_DIR}/zephyr_remote_openocd_config_selection.json")
set(_zro_config_state "${CMAKE_CURRENT_BINARY_DIR}/zephyr_remote_openocd_config_state.json")
execute_process(
  COMMAND "${PYTHON_EXECUTABLE}" "${ZEPHYR_CURRENT_CMAKE_DIR}/config_default.py"
          "${_zro_config_selection}" "${_zro_config_state}"
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
# Encode literal semicolons only for this list-valued property.
string(REPLACE ";" "\\;" _zro_config_dependency "${_zro_config}")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${_zro_config_dependency}")

# Track creation and deletion without interpreting glob characters in the path.
set(_zro_config_glob "${_zro_config}")
string(REPLACE "[" "[[]" _zro_config_glob "${_zro_config_glob}")
string(REPLACE "*" "[*]" _zro_config_glob "${_zro_config_glob}")
string(REPLACE "?" "[?]" _zro_config_glob "${_zro_config_glob}")
file(GLOB _zro_config_presence
     CONFIGURE_DEPENDS
     LIST_DIRECTORIES false
     "${_zro_config_glob}")

# The dependency and glob above detect edits, creation, and deletion only when
# a timestamp advances or the matched name changes. They miss a replacement
# that is not newer than the generated build system, such as moving an older
# file into place or retargeting a symbolic link in the selected path. The
# state record, written at configuration, is rewritten by config_signature.py
# only when the outcome of reading the selected path changes, including when
# the record is missing. The selection record, also written at configuration,
# names that path so no command line has to carry it. It is never a build
# output, so cleaning cannot remove it with the state record.
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${_zro_config_state}")
set(_zro_config_check_command
    "${PYTHON_EXECUTABLE}" "${ZEPHYR_CURRENT_CMAKE_DIR}/config_signature.py"
    "${_zro_config_selection}" "${_zro_config_state}")
if(CMAKE_GENERATOR STREQUAL "Ninja")
  # Ninja documents that when build.ninja is an output of a build statement and
  # is out of date, it rebuilds and reloads it before the requested targets.
  # CMake lists every configure dependency as an input of that statement. Its
  # Ninja generator also adds a phony statement that makes a missing input not
  # an error, but it omits inputs that are custom command outputs; this is
  # undocumented, and both behaviors are covered from CMake 3.20.5. Making the
  # state record a custom command output therefore gives it a real statement
  # that Ninja runs before deciding whether build.ninja is stale, so a rewritten
  # record reruns CMake in the same invocation. The command-less symbolic
  # output becomes a phony statement without inputs, which Ninja considers out
  # of date whenever its file is absent. CMake marks the custom command restat,
  # so an unchanged record does not rerun CMake. Cleaning removes the record as
  # a custom command output; recreating it reruns CMake.
  set(_zro_config_check "${CMAKE_CURRENT_BINARY_DIR}/zephyr_remote_openocd_config_check")
  add_custom_command(OUTPUT "${_zro_config_check}" COMMENT "")
  set_source_files_properties("${_zro_config_check}" PROPERTIES SYMBOLIC TRUE)
  add_custom_command(
    OUTPUT "${_zro_config_state}"
    COMMAND ${_zro_config_check_command}
    DEPENDS "${_zro_config_check}"
    COMMENT "Checking Zephyr Remote OpenOCD configuration"
    VERBATIM)
  # CMake emits a custom command only for a target that uses it. Leaving the
  # target out of ALL runs the check only before build.ninja is loaded.
  add_custom_target(zephyr_remote_openocd_config_state DEPENDS "${_zro_config_state}")
else()
  # Other generators check configuration dependencies without running build
  # commands, so a rewritten record cannot rerun CMake in this invocation. Fail
  # before stale runner state is consumed; the next build reruns CMake.
  add_custom_target(zephyr_remote_openocd_config_state ALL
    COMMAND ${_zro_config_check_command} --fail-on-change
    VERBATIM)
endif()

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
