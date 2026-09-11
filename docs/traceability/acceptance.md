# Acceptance Traceability

This matrix maps each acceptance criterion to maintained automated test coverage
and, where applicable, hardware-test coverage. It does not claim that hardware
has been executed recently. Executed validation belongs in a dated record under
`docs/validation/`; PG-012 and PG-013 remain deferred validations.


Status is classified by maintained coverage: **Automated** means permanent unit,
Zephyr, or SSH integration coverage; **Hardware** means a maintained
real-hardware test requiring a configured fixture; **Both** has both forms.
Availability of a fixture does not imply that the hardware test has been
executed. The only deferred criteria are the WSL-specific PG-012 and PG-013
validations.

| Criterion | Status | Permanent evidence |
| --- | --- | --- |
| AC-INTEG-001 | Automated | `TestZephyrIntegration.test_module_discovery_and_in_tree_application_build`; clean-install acceptance |
| AC-INTEG-002 | Automated | `TestZephyrIntegration.test_out_of_tree_application_build` |
| AC-INTEG-003 | Automated | `TestZephyrIntegration.test_runner_registration_is_conditional_and_non_destructive`; clean-install acceptance |
| AC-INTEG-004 | Automated | `TestZephyrIntegration.test_openocd_arguments_are_mirrored_exactly` |
| AC-INTEG-005 | Automated | `TestZephyrIntegration.test_runner_registration_is_conditional_and_non_destructive` |
| AC-INSTALL-001 | Automated | `TestZephyrIntegration.test_clean_install_acceptance_from_git_free_distribution` |
| AC-INSTALL-002 | Automated | `tests/unit/test_setup.py::test_creates_template_and_reports_activation`; clean-install acceptance |
| AC-INSTALL-003 | Automated | `tests/unit/test_setup.py::test_existing_configuration_is_preserved_and_not_chmodded`; clean-install acceptance |
| AC-INSTALL-004 | Automated | `tests/unit/test_setup.py::test_creates_template_and_reports_activation`; clean-install acceptance |
| AC-INSTALL-005 | Automated | setup unit tests; clean-install acceptance |
| AC-INSTALL-006 | Automated | `tests/unit/test_setup.py::test_dependency_status_messages`; clean-install acceptance using West Python |
| AC-SELECT-001 | Automated | `TestZephyrIntegration.test_config_change_regenerates_default_runner`; clean-install acceptance |
| AC-SELECT-002 | Automated | `TestZephyrIntegration.test_config_change_regenerates_default_runner`; clean-install acceptance |
| AC-SELECT-003 | Automated | `TestZephyrIntegration.test_config_change_regenerates_default_runner` |
| AC-FLASH-001 | Hardware | `TestRealOpenOcdFlash.test_configured_target_flashes_and_emits_fresh_serial_output` (quiet precondition image followed by selected-image output) |
| AC-DEBUG-001 | Hardware | `TestRealOpenOcdDebug.test_debug` (load, continue, configured breakpoint, PC/instruction inspection, detach), `test_attach` (no load, PC/instruction inspection), and `test_debugserver` (independent client halt/resume) |
| AC-DEBUG-002 | Both | `TestDebugPlanning.test_disabled_services_and_distinct_gdb_ports`; `TestRealOpenOcdDebug.test_debugserver` |
| AC-RTT-001 | Both | `TestRttClient.test_bidirectional_non_tty_channel`; all three `TestRealRtt` command variants |
| AC-RTT-002 | Both | `TestZephyrIntegration.test_recording_rtt_command_construction_without_io`; `TestRealRtt.test_standalone_rtt` |
| AC-SEMI-001 | Both | `TestZephyrIntegration.test_recording_direct_semihosting_commands_without_io`; `TestRealSemihosting.test_direct_semihosting_console_normal_completion` |
| AC-CONC-001 | Automated | `TestSshTransportIntegration.test_concurrent_fake_sessions_isolate_identical_remote_ports` |
| AC-LIFE-001 | Both | `TestRealProcessHelper.test_output_exit_status_and_workspace_cleanup`; real flash/debug/RTT/semihosting cleanup assertions |
| AC-LIFE-002 | Automated | `TestRealProcessHelper.test_helper_eof_cleans_child_and_workspace`; `TestSshTransportIntegration.test_helper_ssh_loss_cleans_fake_session` |
| AC-PLAT-001 | Both | native-Linux Zephyr/SSH integration plus real flash and debug fixtures |
| AC-PLAT-002 | Deferred | PG-012 (WSL Linux SSH) and PG-013 (Windows `ssh.exe` from WSL 2) |
| AC-SSH-001 | Automated | `tests/unit/test_ssh.py`; `TestLinuxSshIntegration.test_configured_linux_ssh_and_fixed_arguments` |
| AC-SSH-002 | Deferred | PG-013 / `TestWslSshIntegration.test_windows_ssh_exe_from_wsl` |
| AC-SSH-003 | Automated | `tests/unit/test_ssh.py::test_fixed_arguments_are_preserved_without_a_shell`; `TestLinuxSshIntegration.test_configured_linux_ssh_and_fixed_arguments` |

This table records the current acceptance status. The following sections
summarize implementation, validation, and compatibility status by requirement.
No other acceptance criterion is intentionally untested.
