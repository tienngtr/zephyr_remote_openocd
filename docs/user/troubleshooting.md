# Troubleshooting

If setup fails, check that the module copy is readable, the configuration parent
is writable, and the command is running with Python 3.12 or newer. Missing
`pyelftools` is a warning during setup; use the Python environment configured
for Zephyr before running a command that inspects an ELF.

For remote operations, verify the configured SSH argv can connect directly and
that `[remote].openocd` is an executable absolute path on the remote host. Use
`ssh` command options in `[ssh].command` for fixed arguments (for example,
`ControlMaster=no`); do not put shell pipelines in the value. Path mappings
must point to existing local inputs and normalized remote POSIX destinations.

The helper deploys under the remote user's private runtime/cache directory and
removes session workspaces on normal exit, failure, interruption, or controller
loss. A failure message containing a session workspace or forwarding endpoint
is useful diagnostic evidence; do not remove another user's workspace.

Recording mode (`ZEPHYR_REMOTE_OPENOCD_RECORD=1`) is deliberately no-I/O. It
is useful for checking generated commands and configuration without requiring
SSH, OpenOCD, GDB, or hardware.
