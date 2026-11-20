# Portable wireless control

See [`../docs/HOST.md`](../docs/HOST.md) for setup, configuration, a non-Hermes client example, test commands, deployment cautions, and feature limitations.

This directory implements the host control protocol and CLI only. It contains no firmware, keys, media, or model assets. The default network bind is loopback; configure peer identity and host/port through `SIMSTIM_*` environment variables. Physical-device compatibility depends on matching firmware implementing the same protocol.
