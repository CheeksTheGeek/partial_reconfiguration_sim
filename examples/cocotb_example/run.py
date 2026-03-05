#!/usr/bin/env python3
"""
Run the cocotb PR simulation example.

Usage:
    cd examples/cocotb_example
    uv run python run.py
"""
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

THIS_DIR = Path(__file__).resolve().parent
MULTI_DIR = THIS_DIR.parent / 'multi'

from partial_reconfiguration import PRSystem

os.chdir(MULTI_DIR)


def main():
    with PRSystem(config='pr_config.yaml') as system:
        print("Building RM binaries (cocotb mode — no static binary)...")
        system.build(cocotb_mode=True)
        print("Build complete.")

        print("Running cocotb simulation...")
        system.simulate_cocotb(
            test_module='test_cocotb',
            test_dir=str(THIS_DIR),
        )
        print("Done.")


if __name__ == '__main__':
    sys.exit(main())
