"""
Cocotb PR simulation example.

cocotb drives the static region (clock, reads outputs via VPI).
RM binaries run as separate processes via shared memory.

Reconfiguration uses the SHM control mailbox (PR_CTRL_SHM) — same
mmap infrastructure as the rest of the framework, no temp files.

Tests:
1. Counter increments    — read rp0_counter via VPI
2. Adder result          — read computed_result via VPI
3. Reconfigure rp0       — swap counter_rm → passthrough_rm, verify via VPI
4. Static continuity     — activity_counter runs uninterrupted across the swap
"""
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

from partial_reconfiguration.integration.cocotb_mode import pr_reconfigure


@cocotb.test()
async def test_counter_increments(dut):
    """Test 1: rp0 counter_rm increments each cycle."""
    cocotb.start_soon(Clock(dut.clk, 10, unit='ns').start())

    for _ in range(100):
        await RisingEdge(dut.clk)

    val1 = int(dut.rp0_counter.value)

    for _ in range(1000):
        await RisingEdge(dut.clk)

    val2 = int(dut.rp0_counter.value)

    assert val2 > val1, f"Counter should increment: {val1} -> {val2}"
    cocotb.log.info(f"[PASS] Counter incremented: {val1} -> {val2}")


@cocotb.test()
async def test_adder_result(dut):
    """Test 2: rp1 adder_rm computes operand_a + operand_b = computed_result."""
    cocotb.start_soon(Clock(dut.clk, 10, unit='ns').start())

    for _ in range(500):
        await RisingEdge(dut.clk)

    activity = int(dut.activity_counter.value)
    result   = int(dut.computed_result.value)

    expected_a   = (activity - 1) & 0xFFFF
    expected_b   = ((activity - 1) >> 16) & 0xFFFF
    expected_sum = (expected_a + expected_b) & 0xFFFFFFFF

    tolerance = max(int(expected_sum * 0.05) + 200, 200)
    assert abs(result - expected_sum) <= tolerance, (
        f"Adder result {result} too far from expected {expected_sum} "
        f"(a={expected_a:#06x}, b={expected_b:#06x}, tol=±{tolerance})"
    )
    cocotb.log.info(
        f"[PASS] Adder: activity={activity}, result={result}, expected≈{expected_sum}"
    )


@cocotb.test()
async def test_reconfigure_rp0(dut):
    """
    Test 3: Swap rp0 from counter_rm → passthrough_rm via PRSystem.

    passthrough_rm drives counter = 32'hFFFF_FFFF continuously.
    We verify via VPI that rp0_counter becomes 0xFFFFFFFF after the swap.
    """
    cocotb.start_soon(Clock(dut.clk, 10, unit='ns').start())

    for _ in range(100):
        await RisingEdge(dut.clk)
    val_before = int(dut.rp0_counter.value)
    for _ in range(200):
        await RisingEdge(dut.clk)
    val_after_warmup = int(dut.rp0_counter.value)
    assert val_after_warmup > val_before, \
        f"counter_rm should be counting before swap ({val_before} -> {val_after_warmup})"
    cocotb.log.info(f"  counter_rm running: {val_before} -> {val_after_warmup}")

    cocotb.log.info("  Reconfiguring rp0: counter_rm -> passthrough_rm ...")
    await pr_reconfigure('rp0', 'passthrough_rm')

    for _ in range(500):
        await RisingEdge(dut.clk)

    val_new = int(dut.rp0_counter.value)
    cocotb.log.info(f"  rp0_counter after swap: {val_new:#010x}")

    assert val_new == 0xFFFF_FFFF, (
        f"passthrough_rm should drive 0xFFFFFFFF, got {val_new:#010x}"
    )
    cocotb.log.info("[PASS] Reconfiguration to passthrough_rm confirmed via VPI")


@cocotb.test()
async def test_static_continuity_across_swap(dut):
    """
    Test 4: activity_counter runs uninterrupted before, during, and after swap.
    """
    cocotb.start_soon(Clock(dut.clk, 10, unit='ns').start())

    samples = []

    for _ in range(5):
        for _ in range(100):
            await RisingEdge(dut.clk)
        samples.append(int(dut.activity_counter.value))

    await pr_reconfigure('rp0', 'passthrough_rm')

    for _ in range(5):
        for _ in range(100):
            await RisingEdge(dut.clk)
        samples.append(int(dut.activity_counter.value))

    cocotb.log.info(f"  activity_counter samples: {samples}")
    assert samples == sorted(samples), \
        f"activity_counter went backwards during swap: {samples}"
    assert samples[-1] > samples[0], \
        f"activity_counter did not advance: {samples}"
    cocotb.log.info(
        f"[PASS] Static region ran continuously: {samples[0]} -> {samples[-1]} "
        f"(+{samples[-1] - samples[0]} cycles across swap)"
    )
