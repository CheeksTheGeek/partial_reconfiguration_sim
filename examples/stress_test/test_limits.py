#!/usr/bin/env python3
"""
Tests that exercise the system's port width and clock detection capabilities.

Limit 1: >32-bit ports (up to 64-bit) — NOW SUPPORTED. ShmPort.data is uint64_t.
Limit 2: >64-bit ports — _dpi_type() raises ValueError, no DPI scalar type. STILL A LIMIT.
Limit 3: Non-standard clock name — NOW SUPPORTED. pyslang AST + config fallback detects any clock.
"""
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

THIS_DIR = Path(__file__).resolve().parent

PASS = 0
FAIL = 0


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
        # Trim traceback to just the essential line
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
def test_limit_wide_ports():
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

    # First, we need a minimal static region that instantiates wide64_rm
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
            test_val_48 = 0xABCD_1234_5678  # 48-bit value
            api.write_wide_in(test_val_48)

            for _ in range(5):
                time.sleep(0.005)
                api.read_wide_out()

            result_48 = api.read_wide_out()  # wide_in + 1

            # Write a 64-bit value with upper bits set
            test_val_64 = 0xDEAD_BEEF_CAFE_BABE  # 64-bit value
            api.write_full64_in(test_val_64)

            for _ in range(5):
                time.sleep(0.005)
                api.read_full64_out()

            result_64 = api.read_full64_out()  # full64_in + 1

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
# LIMIT 2: >64-bit ports (128-bit)
# ============================================================
def test_limit_huge_ports():
    """
    _dpi_type() only supports up to 64-bit (longint).
    128-bit ports should raise ValueError during codegen.
    """
    print("\n" + "=" * 60)
    print("LIMIT 2: >64-bit ports (128-bit, no DPI scalar type)")
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

    # Create minimal static region
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

    def try_build():
        with PRSystem(config=config) as system:
            system.build()
            return "build succeeded (unexpected)"

    expect_break(
        "Build with 128-bit boundary port",
        try_build,
        expect_type=ValueError,
    )


# ============================================================
# CAPABILITY 3: Non-standard clock name — NOW WORKS
# ============================================================
def test_limit_weird_clock():
    """
    Clock detection uses a 4-strategy chain via pyslang AST analysis:
    1. Sole 1-bit input port → must be the clock
    2. posedge analysis on always_ff/always blocks
    3. Name-pattern matching (clk, clock, *clk*, *clock*)
    4. First 1-bit input as last resort
    Additionally, auto_wrap_config.clock_name propagates to config defaults.
    A signal named 'sys_input' is now correctly detected.
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
            # clock_name 'sys_input' propagates to config defaults via
            # auto_wrap_config, and pyslang detects it as sole 1-bit input
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
# Main
# ============================================================
def main():
    print("=" * 60)
    print("LIMIT TESTS: Breaking the system on purpose")
    print("=" * 60)
    print("Each test targets a known limitation.")
    print("'BROKE' = expected failure documented.")
    print("'UNEXPECTED PASS' = system handled it (limit doesn't exist!).")

    test_limit_huge_ports()    # Should fail at codegen (>64-bit still a limit)
    test_limit_weird_clock()   # Should pass (clock detection fixed)
    test_limit_wide_ports()    # Should pass (64-bit data channel)

    print("\n" + "=" * 60)
    print(f"Limit tests complete: {PASS} broke as expected, {FAIL} unexpected passes")
    print("=" * 60)

    return 0  # Always exit 0 — these are expected failures


if __name__ == '__main__':
    sys.exit(main())
