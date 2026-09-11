# Troubleshooting

If setup fails, check that the module copy is readable, the configuration parent
is writable, and the command is running with Python 3.12 or newer. Missing
`pyelftools`, PyYAML, or jsonschema produces a setup warning. Use the Python
environment configured for Zephyr 4.4; the runner has no separate product
dependency-installation step.

For remote operations, verify the configured SSH argv can connect directly and
that the selected remote has an executable `openocd_command`. Use
`ssh_command` for fixed SSH arguments (for example,
`ControlMaster=no`); do not put shell pipelines in the value. Path mappings
must point to existing local inputs and normalized remote POSIX destinations.

The helper deploys under the remote user's private runtime or cache directory.
It removes session workspaces after normal exit, failure, interruption, or loss
of the SSH control connection. A failure message containing a session workspace
or forwarding endpoint is useful diagnostic evidence; do not remove another
user's workspace.

Contributors can set `ZRO_RECORD=1` to inspect the runner's generated JSON plan
without starting SSH, OpenOCD, GDB, forwarding, or hardware access. This checks
plan construction, not deployment or real command behavior. See
[Runner recording mode](../development/testing.md#runner-recording-mode) for
usage, limitations, and the optional injected OpenOCD version.

## Common symptoms

**`remote_openocd` is not listed by `west flash --context`.** Ensure the module
is in `EXTRA_ZEPHYR_MODULES`, then rebuild or regenerate the application so west
reloads its runners.

**No remote is selected.** Pass `--remote NAME`, set
`ZEPHYR_REMOTE_OPENOCD_REMOTE`, or configure `default_remote`. The selected
name must exist in the YAML file.

**SSH works manually but the runner fails.** Check the complete configured
`ssh_command`, including fixed options, and verify that remote `python3` and
the configured OpenOCD executable are available to that SSH environment.

**Mapped input is missing remotely.** Remove the mapping to stage the local
input automatically, or install the resource at the mapped remote path and
ensure its path is normalized.

**A GDB client cannot connect.** Use the local endpoint printed by
`debugserver` or an RTT-server operation. Check that the client uses the local
forwarded port, not the remote OpenOCD port, and that the west process is still
running.

**Editing the configuration has no effect.** Re-run `west build` after changing
the default runner or use explicit `-r remote_openocd`; west stores runner
selection in generated build metadata.
