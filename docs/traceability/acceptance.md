# Acceptance Traceability

This matrix maps each acceptance criterion to maintained automated test coverage
and, where applicable, hardware-test coverage. It does not claim that hardware
has been executed recently.

Status is classified by maintained coverage: **Automated** means unit,
Zephyr, or SSH integration coverage; **Hardware** means a maintained
real-hardware test requiring a configured fixture; **Both** has both forms.
Availability of a fixture does not imply that the hardware test has been
executed.

| Criterion | Status | Maintained tests |
| --- | --- | --- |
| AC-INTEG-001 | Automated | [`TestZephyrIntegration.test_module_discovery_and_in_tree_application_build`](../../tests/zephyr_integration/test_zephyr_integration.py); clean-install acceptance |
| AC-INTEG-002 | Automated | [`TestZephyrIntegration.test_out_of_tree_application_build`](../../tests/zephyr_integration/test_zephyr_integration.py) |
| AC-INTEG-003 | Automated | [`TestZephyrIntegration.test_runner_registration_is_conditional_and_non_destructive`](../../tests/zephyr_integration/test_zephyr_integration.py) |
| AC-INTEG-004 | Automated | [`TestZephyrIntegration.test_openocd_arguments_are_mirrored_exactly`](../../tests/zephyr_integration/test_zephyr_integration.py) |
| AC-INTEG-005 | Automated | [`TestZephyrIntegration.test_runner_registration_is_conditional_and_non_destructive`](../../tests/zephyr_integration/test_zephyr_integration.py) |
| AC-INSTALL-001 | Automated | [`TestZephyrIntegration.test_clean_install_acceptance_from_git_free_distribution`](../../tests/zephyr_integration/test_zephyr_integration.py) |
| AC-INSTALL-002 | Automated | [`test_creates_template_and_reports_activation`](../../tests/unit/test_setup.py) |
| AC-INSTALL-003 | Automated | [`test_existing_configuration_is_preserved_and_not_chmodded`](../../tests/unit/test_setup.py) |
| AC-INSTALL-004 | Automated | [`test_creates_template_and_reports_activation`](../../tests/unit/test_setup.py) |
| AC-INSTALL-005 | Automated | [Setup unit tests](../../tests/unit/test_setup.py) |
| AC-INSTALL-006 | Automated | [`test_missing_dependency_warns_without_blocking_initialization`](../../tests/unit/test_setup.py) |
| AC-CONFIG-001 | Automated | [`test_canonical_template_loads`](../../tests/unit/test_config.py); [`test_creates_template_and_reports_activation`](../../tests/unit/test_setup.py) |
| AC-CONFIG-002 | Automated | [Configuration validator unit tests](../../tests/unit/test_validate_configuration.py) |
| AC-SELECT-001 | Automated | [`TestZephyrIntegration.test_config_change_regenerates_default_runner`](../../tests/zephyr_integration/test_zephyr_integration.py) |
| AC-SELECT-002 | Automated | [`TestZephyrIntegration.test_config_change_regenerates_default_runner`](../../tests/zephyr_integration/test_zephyr_integration.py) |
| AC-SELECT-003 | Automated | [`TestZephyrIntegration.test_config_change_regenerates_default_runner`](../../tests/zephyr_integration/test_zephyr_integration.py) |
| AC-FLASH-001 | Hardware | [`TestRealOpenOcdFlash.test_configured_target_flashes_and_emits_fresh_serial_output`](../../tests/hardware/test_real_flash.py) (quiet precondition image followed by selected-image output) |
| AC-DEBUG-001 | Hardware | [`TestRealOpenOcdDebug`](../../tests/hardware/test_real_debug.py): `test_debug` (load, continue, configured breakpoint, PC/instruction inspection, detach), `test_attach` (no load, PC/instruction inspection), and `test_debugserver` (independent client halt/resume) |
| AC-DEBUG-002 | Both | [`TestDebugPlanning.test_disabled_services_and_distinct_gdb_ports`](../../tests/unit/test_remote.py); [`TestRealOpenOcdDebug.test_debugserver`](../../tests/hardware/test_real_debug.py) |
| AC-RTT-001 | Both | [`TestRttClient.test_bidirectional_non_tty_channel`](../../tests/local_integration/test_remote_process.py); all three [`TestRealRtt`](../../tests/hardware/test_real_rtt.py) command variants |
| AC-RTT-002 | Both | [`TestDebugPlanning`](../../tests/unit/test_remote.py); adapter recording coverage; [`TestRealRtt.test_standalone_rtt`](../../tests/hardware/test_real_rtt.py) |
| AC-SEMI-001 | Both | Upstream option and adapter recording coverage; [`TestRealSemihosting.test_direct_semihosting_console_normal_completion`](../../tests/hardware/test_real_semihosting.py) |
| AC-CONC-001 | Automated | [`TestSshTransportIntegration.test_concurrent_sessions_isolate_identical_remote_ports`](../../tests/ssh_integration/test_ssh_integration.py) |
| AC-LIFE-001 | Automated | [`TestRealProcessHelper.test_output_exit_status_and_workspace_cleanup`](../../tests/local_integration/test_remote_process.py); [`TestSshTransportIntegration.test_protocol_v1_helper_vertical_slice`](../../tests/ssh_integration/test_ssh_integration.py) |
| AC-LIFE-002 | Automated | [`TestRealProcessHelper.test_helper_eof_cleans_child_and_workspace`](../../tests/local_integration/test_remote_process.py); [`TestSshTransportIntegration.test_helper_ssh_loss_cleans_session`](../../tests/ssh_integration/test_ssh_integration.py) |
| AC-PLAT-001 | Both | Linux [Zephyr](../../tests/zephyr_integration/) and [SSH](../../tests/ssh_integration/) integration plus [real hardware](../../tests/hardware/) fixtures |
| AC-SSH-001 | Automated | [SSH unit tests](../../tests/unit/test_ssh.py); [`TestConfiguredSshIntegration.test_configured_ssh_and_fixed_arguments`](../../tests/ssh_integration/test_ssh_integration.py) |
| AC-SSH-002 | Automated | [SSH command tests](../../tests/unit/test_ssh.py); [`TestConfiguredSshIntegration`](../../tests/ssh_integration/test_ssh_integration.py) fixed-argument and alternate-name cases; [`TestSshTransportIntegration.test_forwarding_and_session_lifecycle_use_configured_client`](../../tests/ssh_integration/test_ssh_integration.py) |

This table records the current acceptance status.
