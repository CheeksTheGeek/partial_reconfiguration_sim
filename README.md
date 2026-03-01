# Partial Reconfiguration Simulation Framework

Multi-process Verilator simulation of FPGA partial reconfiguration using DPI-C bridges and shared memory.

See [USAGE.md](USAGE.md) for detailed architecture documentation.

## Quick Start

```console
uv sync
uv run examples/blinking_led/test.py
uv run examples/multi/test.py
```

