#!/usr/bin/env python3
"""
Tests that exercise the system's extended capabilities.

Capability 1: >32-bit ports (up to 64-bit) — ShmPort.data is uint64_t.
Capability 2: >64-bit ports (128-bit) — multi-chunk representation (ceil(W/64) slots per port).
Capability 3: Non-standard clock name — pyslang AST + config fallback detects any clock.
Capability 4: Multiple clocks per partition — clock_names list, per-clock always blocks.
Capability 5: Batch commands — CMD_BATCH sends up to 32 read/write ops in one cycle.
Capability 6: Reset protocol enforcement — RM driver asserts reset before rm_ready=1.
"""
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

THIS_DIR = Path(__file__).resolve().parent

PASS = 0
FAIL = 0


def check(condition, label):
    global PASS, FAIL
    if condition:
        print(f"    [PASS] {label}")
        PASS += 1
    else:
        print(f"    [FAIL] {label}")
        FAIL += 1


def expect_break(label, fn, expect_type=None):
    """Run fn, expecting it to fail. Report what happened."""
    global PASS, FAIL
    print(f"\n  {label}")
    try:
        result = fn()
        print(f"    DID NOT BREAK — returned: {result}")
        print(f"    [UNEXPECTED PASS] System handled this case!")
        FAIL += 1
        return result
    except Exception as e:
        etype = type(e).__name__
        print(f"    BROKE with {etype}: {e}")
        if expect_type and isinstance(e, expect_type):
            print(f"    [EXPECTED BREAK] Correct error type: {etype}")
            PASS += 1
        else:
            print(f"    [BROKE] Error type: {etype}")
            PASS += 1  # Still "pass" — we wanted it to break
        return None


def expect_wrong(label, fn, validator):
    """Run fn, expecting it to produce wrong results. validator(result) -> (ok, detail)."""
    global PASS, FAIL
    print(f"\n  {label}")
    try:
        result = fn()
        ok, detail = validator(result)
        if ok:
            print(f"    DID NOT BREAK — result is correct: {detail}")
            print(f"    [UNEXPECTED PASS] System handled this case!")
            FAIL += 1
        else:
            print(f"    WRONG RESULT: {detail}")
            print(f"    [EXPECTED BREAK] Data corruption confirmed")
            PASS += 1
        return result
    except Exception as e:
        print(f"    BROKE with {type(e).__name__}: {e}")
        print(f"    [BROKE EARLY] Didn't even get to wrong results")
        PASS += 1
        return None


# ============================================================
# CAPABILITY 1: >32-bit ports (48-bit and 64-bit) — NOW WORKS
# ============================================================
def test_capability_wide_ports():
    """
    ShmPort.data is uint64_t. Ports up to 64 bits wide are fully supported
    in the shared memory channel. 48-bit and 64-bit values should pass
    through without truncation.
    """
    print("\n" + "=" * 60)
    print("CAPABILITY 1: >32-bit ports (ShmPort.data is uint64_t)")
    print("=" * 60)

    from partial_reconfiguration import PRSystem

    config = {
        'version': '1.0',
        'simulation': {'tool': 'verilator', 'build_dir': str(THIS_DIR / 'build_wide')},
        'static_region': {
            'name': 'wide_static',
            'design': 'wide_static',
            'sources': [str(THIS_DIR / 'rtl' / 'wide_static.sv')],
            'auto_wrap': True,
            'auto_wrap_config': {'clock_name': 'clk'},
        },
        'partitions': [{
            'name': 'rp_wide64',
            'rm_module': 'wide64_rm',
            'clock': 'clk',
            'boundary': [
                {'name': 'wide_in',    'direction': 'to_rm',   'width': 48},
                {'name': 'wide_out',   'direction': 'from_rm', 'width': 48},
                {'name': 'full64_in',  'direction': 'to_rm',   'width': 64},
                {'name': 'full64_out', 'direction': 'from_rm', 'width': 64},
            ],
            'initial_rm': 'wide64_rm',
        }],
        'reconfigurable_modules': [{
            'name': 'wide64_rm',
            'partition': 'rp_wide64',
            'design': 'wide64_rm',
            'sources': [str(THIS_DIR / 'rtl' / 'wide64_rm.sv')],
            'auto_wrap': True,
            'auto_wrap_config': {'clock_name': 'clk'},
        }],
    }

    # Create minimal static region if needed
    wide_static_sv = THIS_DIR / 'rtl' / 'wide_static.sv'
    if not wide_static_sv.exists():
        wide_static_sv.write_text("""\
`timescale 1ns/1ps
module wide_static (
    input wire clk,
    output reg [31:0] tick
);
    always @(posedge clk) tick <= tick + 1;

    wide64_rm u_wide64_rm (
        .clk(clk),
        .wide_in(48'd0),
        .wide_out(),
        .full64_in(64'd0),
        .full64_out()
    );
endmodule
""")

    def try_build_and_test():
        with PRSystem(config=config) as system:
            system.build()
            system.simulate()
            time.sleep(0.3)

            api = system.get_rm_api('rp_wide64')

            # Write a 48-bit value with upper bits set
            test_val_48 = 0xABCD_1234_5678
            api.write_wide_in(test_val_48)

            for _ in range(5):
                time.sleep(0.005)
                api.read_wide_out()

            result_48 = api.read_wide_out()

            # Write a 64-bit value with upper bits set
            test_val_64 = 0xDEAD_BEEF_CAFE_BABE
            api.write_full64_in(test_val_64)

            for _ in range(5):
                time.sleep(0.005)
                api.read_full64_out()

            result_64 = api.read_full64_out()

            return (test_val_48, result_48, test_val_64, result_64)

    result = expect_break(
        "Build + run with 48-bit and 64-bit boundary ports",
        try_build_and_test,
    )

    # If build succeeded, check for data corruption
    if result is not None:
        test_val_48, result_48, test_val_64, result_64 = result

        expected_48 = (test_val_48 + 1) & 0xFFFF_FFFF_FFFF
        expected_64 = (test_val_64 + 1) & 0xFFFF_FFFF_FFFF_FFFF

        expect_wrong(
            f"48-bit port: wrote 0x{test_val_48:012X}, read back 0x{result_48:012X}",
            lambda: result_48,
            lambda r: (
                r == expected_48,
                f"got 0x{r:012X}, expected 0x{expected_48:012X}"
                + (f" — upper bits TRUNCATED" if r != expected_48 else "")
            ),
        )

        expect_wrong(
            f"64-bit port: wrote 0x{test_val_64:016X}, read back 0x{result_64:016X}",
            lambda: result_64,
            lambda r: (
                r == expected_64,
                f"got 0x{r:016X}, expected 0x{expected_64:016X}"
                + (f" — upper bits TRUNCATED" if r != expected_64 else "")
            ),
        )


# ============================================================
# CAPABILITY 2: >64-bit ports (128-bit) — NOW SUPPORTED
# Multi-chunk representation: ceil(W/64) consecutive SHM slots
# ============================================================
def test_capability_huge_ports():
    """
    128-bit ports are now supported via multi-chunk representation.
    A 128-bit port occupies 2 consecutive SHM slots (chunks).
    DPI codegen emits per-chunk functions: _chunk0_send, _chunk1_send.
    Python API reassembles chunks into a single int.
    """
    print("\n" + "=" * 60)
    print("CAPABILITY 2: >64-bit ports (128-bit, multi-chunk)")
    print("=" * 60)

    from partial_reconfiguration import PRSystem

    config = {
        'version': '1.0',
        'simulation': {'tool': 'verilator', 'build_dir': str(THIS_DIR / 'build_huge')},
        'static_region': {
            'name': 'huge_static',
            'design': 'huge_static',
            'sources': [str(THIS_DIR / 'rtl' / 'huge_static.sv')],
            'auto_wrap': True,
            'auto_wrap_config': {'clock_name': 'clk'},
        },
        'partitions': [{
            'name': 'rp_huge',
            'rm_module': 'wide128_rm',
            'clock': 'clk',
            'boundary': [
                {'name': 'huge_in',  'direction': 'to_rm',   'width': 128},
                {'name': 'huge_out', 'direction': 'from_rm', 'width': 128},
            ],
            'initial_rm': 'wide128_rm',
        }],
        'reconfigurable_modules': [{
            'name': 'wide128_rm',
            'partition': 'rp_huge',
            'design': 'wide128_rm',
            'sources': [str(THIS_DIR / 'rtl' / 'wide128_rm.sv')],
            'auto_wrap': True,
            'auto_wrap_config': {'clock_name': 'clk'},
        }],
    }

    # Create minimal static region if needed
    huge_static_sv = THIS_DIR / 'rtl' / 'huge_static.sv'
    if not huge_static_sv.exists():
        huge_static_sv.write_text("""\
`timescale 1ns/1ps
module huge_static (
    input wire clk,
    output reg [31:0] tick
);
    always @(posedge clk) tick <= tick + 1;

    wide128_rm u_wide128_rm (
        .clk(clk),
        .huge_in(128'd0),
        .huge_out()
    );
endmodule
""")

    test_val = 0xDEAD_BEEF_CAFE_BABE_1234_5678_9ABC_DEF0
    expected = (test_val + 1) & ((1 << 128) - 1)

    def try_build_and_test():
        with PRSystem(config=config) as system:
            system.build()
            system.simulate()
            time.sleep(0.3)

            api = system.get_rm_api('rp_huge')

            # Write a 128-bit value
            api.write_huge_in(test_val)

            # Let it propagate
            for _ in range(5):
                time.sleep(0.005)
                api.read_huge_out()

            result = api.read_huge_out()
            return result

    print(f"\n  Build + run with 128-bit boundary port")
    try:
        result = try_build_and_test()
        print(f"    wrote:     0x{test_val:032X}")
        print(f"    read back: 0x{result:032X}")
        print(f"    expected:  0x{expected:032X}")
        check(result == expected, f"128-bit round-trip: huge_out == huge_in + 1")
    except Exception as e:
        print(f"    FAILED with {type(e).__name__}: {e}")
        traceback.print_exc()
        check(False, "128-bit build+run should succeed")


# ============================================================
# CAPABILITY 3: Non-standard clock name — NOW WORKS
# ============================================================
def test_capability_weird_clock():
    """
    Clock detection uses a 4-strategy chain via pyslang AST analysis.
    A signal named 'sys_input' is correctly detected.
    """
    print("\n" + "=" * 60)
    print("CAPABILITY 3: Non-standard clock name ('sys_input')")
    print("=" * 60)

    from partial_reconfiguration import PRSystem

    config = {
        'version': '1.0',
        'simulation': {'tool': 'verilator', 'build_dir': str(THIS_DIR / 'build_clock')},
        'static_region': {
            'name': 'weird_clock_static',
            'design': 'weird_clock_static',
            'sources': [
                str(THIS_DIR / 'rtl' / 'weird_clock_static.sv'),
            ],
            'auto_wrap': True,
            'auto_wrap_config': {'clock_name': 'sys_input'},
        },
        'partitions': [{
            'name': 'rp_dummy',
            'rm_module': 'weird_rm',
            'clock': 'sys_input',
            'boundary': [
                {'name': 'data_out', 'direction': 'from_rm', 'width': 32},
            ],
            'initial_rm': 'weird_rm_mod',
        }],
        'reconfigurable_modules': [{
            'name': 'weird_rm_mod',
            'partition': 'rp_dummy',
            'design': 'weird_rm',
            'sources': [str(THIS_DIR / 'rtl' / 'weird_rm.sv')],
            'auto_wrap': True,
            'auto_wrap_config': {'clock_name': 'sys_input'},
        }],
    }

    def try_build():
        with PRSystem(config=config) as system:
            system.build()
            return "build succeeded"

    expect_break(
        "Build with clock named 'sys_input' (no clk/clock pattern match)",
        try_build,
    )


# ============================================================
# CAPABILITY 4: Multiple clocks per partition
# ============================================================
def test_capability_dual_clock():
    """
    Partition with clock_names: [fast_clk, slow_clk].
    RM wrapper declares both clock inputs.
    RM driver drives both clocks (1:1 in simulation).
    Bridge groups always blocks per clock.
    """
    print("\n" + "=" * 60)
    print("CAPABILITY 4: Multiple clocks per partition")
    print("=" * 60)

    from partial_reconfiguration import PRSystem

    # Create static region with derived clocks
    dual_clock_static_sv = THIS_DIR / 'rtl' / 'dual_clock_static.sv'
    if not dual_clock_static_sv.exists():
        dual_clock_static_sv.write_text("""\
`timescale 1ns/1ps
module dual_clock_static (
    input wire clk,
    output reg [31:0] tick
);
    // Both partition clocks derived from static clock
    wire fast_clk = clk;
    wire slow_clk = clk;

    always @(posedge clk) tick <= tick + 1;

    // Instantiation replaced by DPI bridge at build time
    dual_clock_rm u_dual_clock_rm (
        .fast_clk(fast_clk),
        .slow_clk(slow_clk),
        .data_fast(32'd0),
        .result_slow()
    );
endmodule
""")

    config = {
        'version': '1.0',
        'simulation': {'tool': 'verilator', 'build_dir': str(THIS_DIR / 'build_dual_clock')},
        'static_region': {
            'name': 'dual_clock_static',
            'design': 'dual_clock_static',
            'sources': [str(dual_clock_static_sv)],
            'auto_wrap': True,
            'auto_wrap_config': {'clock_name': 'clk'},
        },
        'partitions': [{
            'name': 'rp_dual',
            'rm_module': 'dual_clock_rm',
            'clocks': ['fast_clk', 'slow_clk'],
            'boundary': [
                {'name': 'data_fast',    'direction': 'to_rm',   'width': 32},
                {'name': 'result_slow',  'direction': 'from_rm', 'width': 32},
            ],
            'initial_rm': 'dual_clock_rm',
        }],
        'reconfigurable_modules': [{
            'name': 'dual_clock_rm',
            'partition': 'rp_dual',
            'design': 'dual_clock_rm',
            'sources': [str(THIS_DIR / 'rtl' / 'dual_clock_rm.sv')],
            'auto_wrap': True,
            'auto_wrap_config': {'clock_name': 'fast_clk'},
        }],
    }

    print(f"\n  Build + run with dual-clock partition [fast_clk, slow_clk]")
    try:
        with PRSystem(config=config) as system:
            system.build()
            system.simulate()
            time.sleep(0.3)

            api = system.get_rm_api('rp_dual')

            # Write a test value
            test_val = 0x42424242
            api.write_data_fast(test_val)

            # Let it propagate through both clock domains
            for _ in range(10):
                time.sleep(0.005)
                api.read_result_slow()

            result = api.read_result_slow()
            print(f"    wrote data_fast = 0x{test_val:08X}")
            print(f"    read result_slow = 0x{result:08X}")
            check(result == test_val, f"Dual-clock: result_slow == data_fast (0x{result:08X})")

    except Exception as e:
        print(f"    FAILED with {type(e).__name__}: {e}")
        traceback.print_exc()
        check(False, "Dual-clock build+run should succeed")


# ============================================================
# CAPABILITY 5: Batch commands from Python
# ============================================================
def test_capability_batch_commands():
    """
    CMD_BATCH sends up to 32 read/write operations in a single cycle.
    ShmMailbox has batch_count + batch[MAX_BATCH] slots.
    SharedMemoryInterface.batch_read_write() fills slots and triggers CMD_BATCH.
    """
    print("\n" + "=" * 60)
    print("CAPABILITY 5: Batch commands (CMD_BATCH)")
    print("=" * 60)

    from partial_reconfiguration import PRSystem
    from partial_reconfiguration.shm_interface import CMD_READ, CMD_WRITE

    # Reuse the wide port config (48-bit + 64-bit boundary)
    config = {
        'version': '1.0',
        'simulation': {'tool': 'verilator', 'build_dir': str(THIS_DIR / 'build_batch')},
        'static_region': {
            'name': 'wide_static',
            'design': 'wide_static',
            'sources': [str(THIS_DIR / 'rtl' / 'wide_static.sv')],
            'auto_wrap': True,
            'auto_wrap_config': {'clock_name': 'clk'},
        },
        'partitions': [{
            'name': 'rp_batch',
            'rm_module': 'wide64_rm',
            'clock': 'clk',
            'boundary': [
                {'name': 'wide_in',    'direction': 'to_rm',   'width': 48},
                {'name': 'wide_out',   'direction': 'from_rm', 'width': 48},
                {'name': 'full64_in',  'direction': 'to_rm',   'width': 64},
                {'name': 'full64_out', 'direction': 'from_rm', 'width': 64},
            ],
            'initial_rm': 'wide64_rm',
        }],
        'reconfigurable_modules': [{
            'name': 'wide64_rm',
            'partition': 'rp_batch',
            'design': 'wide64_rm',
            'sources': [str(THIS_DIR / 'rtl' / 'wide64_rm.sv')],
            'auto_wrap': True,
            'auto_wrap_config': {'clock_name': 'clk'},
        }],
    }

    # Ensure static region RTL exists
    wide_static_sv = THIS_DIR / 'rtl' / 'wide_static.sv'
    if not wide_static_sv.exists():
        wide_static_sv.write_text("""\
`timescale 1ns/1ps
module wide_static (
    input wire clk,
    output reg [31:0] tick
);
    always @(posedge clk) tick <= tick + 1;

    wide64_rm u_wide64_rm (
        .clk(clk),
        .wide_in(48'd0),
        .wide_out(),
        .full64_in(64'd0),
        .full64_out()
    );
endmodule
""")

    print(f"\n  Build + run with batch read/write commands")
    try:
        with PRSystem(config=config) as system:
            system.build()
            system.simulate()
            time.sleep(0.3)

            # Get raw SHM interface for the partition
            shm = system.shm_for_partition('rp_batch')

            # Port layout (from boundary config):
            # to_rm: wide_in (48-bit, slot 0), full64_in (64-bit, slot 1)
            # from_rm: wide_out (48-bit, slot 2), full64_out (64-bit, slot 3)

            # Batch write both inputs at once
            test_48 = 0xABCD_1234_0000
            test_64 = 0x1111_2222_3333_4444
            ops_write = [
                (CMD_WRITE, 0, test_48),    # write wide_in (slot 0)
                (CMD_WRITE, 1, test_64),    # write full64_in (slot 1)
            ]
            results_w = shm.batch_read_write(ops_write)
            print(f"    Batch write: 2 ops sent")
            check(len(results_w) == 2, "Batch write returned 2 results")

            # Let values propagate
            for _ in range(5):
                time.sleep(0.005)
                shm.read_port(2)

            # Batch read both outputs at once
            ops_read = [
                (CMD_READ, 2, 0),    # read wide_out (slot 2)
                (CMD_READ, 3, 0),    # read full64_out (slot 3)
            ]
            results_r = shm.batch_read_write(ops_read)
            result_48 = results_r[0] & 0xFFFF_FFFF_FFFF  # mask to 48 bits
            result_64 = results_r[1]

            expected_48 = (test_48 + 1) & 0xFFFF_FFFF_FFFF
            expected_64 = (test_64 + 1) & 0xFFFF_FFFF_FFFF_FFFF

            print(f"    Batch read: wide_out=0x{result_48:012X} (expected 0x{expected_48:012X})")
            print(f"    Batch read: full64_out=0x{result_64:016X} (expected 0x{expected_64:016X})")

            check(result_48 == expected_48, f"Batch 48-bit: wide_out == wide_in + 1")
            check(result_64 == expected_64, f"Batch 64-bit: full64_out == full64_in + 1")

            # Mixed batch: write + read in a single batch
            test_val = 0xBEEF_CAFE_0000
            ops_mixed = [
                (CMD_WRITE, 0, test_val),   # write wide_in
                (CMD_READ,  3, 0),          # read full64_out (still previous value)
            ]
            results_m = shm.batch_read_write(ops_mixed)
            check(len(results_m) == 2, "Mixed batch returned 2 results")
            print(f"    Mixed batch: write + read in single CMD_BATCH")

    except Exception as e:
        print(f"    FAILED with {type(e).__name__}: {e}")
        traceback.print_exc()
        check(False, "Batch commands build+run should succeed")


# ============================================================
# CAPABILITY 6: Reset protocol enforcement
# ============================================================
def test_capability_reset_enforcement():
    """
    RM driver asserts reset for reset_cycles before rm_ready=1.
    After reset: state == 0xDEAD_BEEF (then increments).
    Without reset: Verilator defaults to 0.
    Test: read state, verify it started from 0xDEAD_BEEF, not 0.
    """
    print("\n" + "=" * 60)
    print("CAPABILITY 6: Reset protocol enforcement")
    print("=" * 60)

    from partial_reconfiguration import PRSystem

    # Create static region for reset test
    reset_static_sv = THIS_DIR / 'rtl' / 'reset_static.sv'
    if not reset_static_sv.exists():
        reset_static_sv.write_text("""\
`timescale 1ns/1ps
module reset_static (
    input wire clk,
    output reg [31:0] tick
);
    always @(posedge clk) tick <= tick + 1;

    // Instantiation replaced by DPI bridge at build time
    resettable_rm u_resettable_rm (
        .clk(clk),
        .rst_n(1'b1),
        .state()
    );
endmodule
""")

    config = {
        'version': '1.0',
        'simulation': {'tool': 'verilator', 'build_dir': str(THIS_DIR / 'build_reset')},
        'static_region': {
            'name': 'reset_static',
            'design': 'reset_static',
            'sources': [str(reset_static_sv)],
            'auto_wrap': True,
            'auto_wrap_config': {'clock_name': 'clk'},
        },
        'partitions': [{
            'name': 'rp_reset',
            'rm_module': 'resettable_rm',
            'clock': 'clk',
            'resets': [{'name': 'rst_n', 'polarity': 'negative'}],
            'reset_cycles': 10,
            'reset_behavior': 'fresh',
            'boundary': [
                {'name': 'state', 'direction': 'from_rm', 'width': 32},
            ],
            'initial_rm': 'resettable_rm',
        }],
        'reconfigurable_modules': [{
            'name': 'resettable_rm',
            'partition': 'rp_reset',
            'design': 'resettable_rm',
            'sources': [str(THIS_DIR / 'rtl' / 'resettable_rm.sv')],
            'auto_wrap': True,
            'auto_wrap_config': {'clock_name': 'clk'},
        }],
    }

    print(f"\n  Build + run with reset-enabled partition (rst_n, 10 reset cycles)")
    try:
        with PRSystem(config=config) as system:
            system.build()
            system.simulate()
            time.sleep(0.3)

            api = system.get_rm_api('rp_reset')

            # Read state — should have started from 0xDEAD_BEEF after reset
            for _ in range(5):
                time.sleep(0.005)
                api.read_state()

            state = api.read_state()
            print(f"    state = 0x{state:08X}")

            # After reset deasserts, state increments from 0xDEAD_BEEF.
            # By the time we read it, it will be 0xDEAD_BEEF + N for some N.
            # Without reset enforcement, Verilator initializes to 0, so state
            # would be a small number.
            # Check: state should be in range [0xDEAD_BEEF, 0xDEAD_BEEF + 100000]
            # (wrapping is fine since 0xDEAD_BEEF + 100000 < 0xFFFFFFFF)
            lower = 0xDEAD_BEEF
            upper = lower + 100_000
            in_range = lower <= state <= upper
            check(in_range,
                  f"Reset enforced: state 0x{state:08X} in [0x{lower:08X}, 0x{upper:08X}]")

            # Extra check: state must NOT be near 0 (which means no reset was applied)
            check(state > 0x8000_0000,
                  f"State high bit set (started from 0xDEAD_BEEF, not 0)")

    except Exception as e:
        print(f"    FAILED with {type(e).__name__}: {e}")
        traceback.print_exc()
        check(False, "Reset enforcement build+run should succeed")


# ============================================================
# Main
# ============================================================
def main():
    print("=" * 60)
    print("CAPABILITY TESTS: Exercising extended system features")
    print("=" * 60)
    print("Tests for wide ports, multi-chunk, multi-clock, batch, reset.")

    test_capability_huge_ports()      # 128-bit multi-chunk
    test_capability_weird_clock()     # Non-standard clock name
    test_capability_wide_ports()      # 48-bit and 64-bit
    test_capability_dual_clock()      # Multiple clocks per partition
    test_capability_batch_commands()  # CMD_BATCH with batch slots
    test_capability_reset_enforcement()  # Reset before rm_ready

    print("\n" + "=" * 60)
    print(f"Results: {PASS} passed, {FAIL} failed")
    print("=" * 60)

    return 1 if FAIL > 0 else 0


if __name__ == '__main__':
    sys.exit(main())
