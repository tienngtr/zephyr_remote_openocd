# Project Background

This document records the historical motivation for Zephyr Remote OpenOCD.
It is non-normative: it does not define product requirements, constrain the
supported deployment models, or make the original environment a required
topology.

## Origin

The project originated in a development environment where target boards were
attached to a shared central machine. Developers connected to that machine
over SSH and performed most development work there, including editing source,
running language-server tooling, building firmware, and invoking Zephyr
commands that accessed the attached boards.

This workflow kept board access straightforward and established familiar habits
around commands such as `west flash`, `west debug`, and related runner
operations.

## Scaling Pressure

The central-machine workflow scaled poorly as the number of developers grew.
Editing, language-server analysis, builds, and other development workloads all
consumed resources on the same machine. Under increasing concurrent load, the
central machine became less responsive.

At the same time, each developer already had a local PC with substantial CPU,
memory, and storage capacity that was largely unused by those workloads. Moving
the ordinary development workload to local PCs could distribute the resource
demand while leaving the central machine responsible for the hardware that
could not be moved to each developer.

## Design Influence

Zephyr Remote OpenOCD grew from that pressure: keep source editing,
language-server tooling, builds, west, GDB, and other development activity on
the local machine while using the remote host for OpenOCD and the physically
attached boards.

The earlier SSH-based workflow also influenced usability. Developers were
accustomed to ordinary Zephyr commands rather than selecting a special remote
backend on every invocation. This makes preserving normal west runner workflows,
including the ability to configure `remote_openocd` as the default runner, an
important design consideration even though the replacement cannot be perfectly
transparent.

These origins explain several current design drivers, but they do not limit the
project to a shared central host or to the original development environment.
The supported behavior and deployment constraints are defined by the
[Software Requirements Specification](requirements/SRS.md), while current
architectural decisions are described by the
[Software Architecture Document](architecture/SAD.md).
