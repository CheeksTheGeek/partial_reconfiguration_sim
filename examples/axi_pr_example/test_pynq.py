"""
AXI PR example — cocotbpynq integration test.

This file runs UNCHANGED on:
  - A real PYNQ board  (import pynq; python test_pynq.py)
  - Simulation        (via run.py  → system.simulate_cocotb())

Test flow:
  1. Write 0x1234 to data_in register (offset 0x00).
  2. Read result register (offset 0x04).
     adder_rm computes  result = data_in + 0x1000 → expect 0x2234.
  3. Reconfigure rp_compute → inverter_rm.
  4. Write 0x1234 again.
  5. Read result.
     inverter_rm computes  result = ~data_in → expect 0xFFFF_EDCB.
"""
import os
import sys

COCOTB_IS_RUNNING = os.environ.get('COCOTB_IS_RUNNING') == '1'

if not COCOTB_IS_RUNNING:
    # Real PYNQ board
    from pynq import Overlay, MMIO
else:
    # Patch cocotb 2.0 to restore the API cocotbpynq needs, BEFORE importing it
    from partial_reconfiguration.integration.cocotb_mode import (
        _patch_cocotb_for_v2, PROverlay as Overlay, pr_synctest,
    )
    _patch_cocotb_for_v2()
    from cocotbpynq import MMIO

IP_BASE  = 0x43C00000
IP_RANGE = 0x10000

DATA_IN_OFFSET = 0x00
RESULT_OFFSET  = 0x04


def main(dut=None):
    overlay = Overlay('design.bit')
    mmio    = MMIO(IP_BASE, IP_RANGE)

    # ── Test 1: adder_rm (result = data_in + 0x1000) ──────────────────────
    mmio.write(DATA_IN_OFFSET, 0x1234)
    result = mmio.read(RESULT_OFFSET)
    expected = (0x1234 + 0x1000) & 0xFFFF_FFFF
    assert result == expected, \
        f"adder_rm: expected {expected:#010x}, got {result:#010x}"
    print(f"[PASS] adder_rm: 0x1234 + 0x1000 = {result:#010x}")

    # ── Reconfigure to inverter_rm ─────────────────────────────────────────
    print("Reconfiguring rp_compute: adder_rm → inverter_rm ...")
    overlay.reconfigure('rp_compute', 'inverter_rm')
    print("Reconfiguration complete.")

    # ── Test 2: inverter_rm (result = ~data_in) ────────────────────────────
    mmio.write(DATA_IN_OFFSET, 0x1234)
    result = mmio.read(RESULT_OFFSET)
    expected = (~0x1234) & 0xFFFF_FFFF
    assert result == expected, \
        f"inverter_rm: expected {expected:#010x}, got {result:#010x}"
    print(f"[PASS] inverter_rm: ~0x1234 = {result:#010x}")

    # ── Reconfigure back to adder_rm ───────────────────────────────────────
    print("Reconfiguring rp_compute: inverter_rm → adder_rm ...")
    overlay.reconfigure('rp_compute', 'adder_rm')
    print("Reconfiguration complete.")

    # ── Test 3: adder_rm again (verify round-trip) ─────────────────────────
    mmio.write(DATA_IN_OFFSET, 0xABCD)
    result = mmio.read(RESULT_OFFSET)
    expected = (0xABCD + 0x1000) & 0xFFFF_FFFF
    assert result == expected, \
        f"adder_rm (round-trip): expected {expected:#010x}, got {result:#010x}"
    print(f"[PASS] adder_rm round-trip: 0xABCD + 0x1000 = {result:#010x}")

    print("\n[PASS] All tests passed.")


if __name__ == "__main__":
    main()
elif COCOTB_IS_RUNNING:
    main = pr_synctest(main)
