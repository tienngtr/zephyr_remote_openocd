# SPDX-License-Identifier: Apache-2.0

"""Zephyr west runner integration.

The versioned zephyr44.runner and zephyr45.runner facades expose runner classes.
The external west entry point selects the active source adapter on each invocation.
Other modules are implementation interfaces, not a supported Python SDK.
"""
