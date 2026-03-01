#!/usr/bin/env python3
"""
Stress test for partial reconfiguration system.

Tests:
1. Mixed-width boundary ports (1, 8, 16, 32 bits)
2. Many-port boundary (4 in + 4 out = 8 ports)
3. Reconfiguration to alternate RMs
4. Rapid back-and-forth reconfiguration
5. Python API read/write across all widths
6. Static region continuity during swaps
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from partial_reconfiguration import PRSystem

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


def test_mixed_width_basic(system):
    """Test 1: Read/write mixed-width ports via Python API."""
    print("\n--- Test 1: Mixed-width boundary ports ---")
    api = system.get_rm_api('rp_wide')

    # Write to each width
    api.write_enable(1)
    api.write_byte_in(0xAB)
    api.write_half_in(0x1234)
    api.write_word_in(0xDEADBEEF)

    # Let values propagate
    for _ in range(5):
        time.sleep(0.005)
        flag = api.read_flag_out()

    byte_out = api.read_status_byte()
    half_out = api.read_result_half()
    word_out = api.read_result_word()

    print(f"    flag_out    = {flag} (1-bit)")
    print(f"    status_byte = 0x{byte_out:02X} (8-bit, expected 0xAC)")
    print(f"    result_half = 0x{half_out:04X} (16-bit, expected 0x1298)")
    print(f"    result_word = 0x{word_out:08X} (32-bit, expected 0xBD5B7DDE)")

    check(flag == 1, "1-bit flag_out = 1 (OR-reduce of 0xAB)")
    check(byte_out == 0xAC, f"8-bit status_byte = 0x{byte_out:02X} (0xAB + 1)")
    check(half_out == 0x1298, f"16-bit result_half (0x1234 + 100 = 0x1298)")
    check(word_out == 0xBD5B7DDE, f"32-bit result_word (0xDEADBEEF * 2)")


def test_mixed_width_zero(system):
    """Test 2: All-zero inputs with enable=0."""
    print("\n--- Test 2: Mixed-width with enable=0 ---")
    api = system.get_rm_api('rp_wide')

    api.write_enable(0)
    api.write_byte_in(0xFF)
    api.write_half_in(0xFFFF)
    api.write_word_in(0xFFFFFFFF)

    for _ in range(5):
        time.sleep(0.005)
        api.read_flag_out()

    flag = api.read_flag_out()
    byte_out = api.read_status_byte()
    half_out = api.read_result_half()
    word_out = api.read_result_word()

    check(flag == 0, "flag_out = 0 when enable=0")
    check(byte_out == 0, "status_byte = 0 when enable=0")
    check(half_out == 0, "result_half = 0 when enable=0")
    check(word_out == 0, "result_word = 0 when enable=0")


def test_many_ports_basic(system):
    """Test 3: Many-port boundary (4+4)."""
    print("\n--- Test 3: Many-port boundary (4 in + 4 out) ---")
    api = system.get_rm_api('rp_many')

    api.write_in_0(100)
    api.write_in_1(200)
    api.write_in_2(300)
    api.write_in_3(400)

    for _ in range(5):
        time.sleep(0.005)
        api.read_out_0()

    out_0 = api.read_out_0()  # in_0 + in_1 = 300
    out_1 = api.read_out_1()  # in_2 + in_3 = 700
    out_2 = api.read_out_2()  # in_0 ^ in_2
    out_3 = api.read_out_3()  # in_1 ^ in_3

    print(f"    out_0 = {out_0} (expected 300 = 100+200)")
    print(f"    out_1 = {out_1} (expected 700 = 300+400)")
    print(f"    out_2 = {out_2} (expected {100 ^ 300} = 100^300)")
    print(f"    out_3 = {out_3} (expected {200 ^ 400} = 200^400)")

    check(out_0 == 300, "out_0 = in_0 + in_1")
    check(out_1 == 700, "out_1 = in_2 + in_3")
    check(out_2 == (100 ^ 300), "out_2 = in_0 ^ in_2")
    check(out_3 == (200 ^ 400), "out_3 = in_1 ^ in_3")


def test_reconfig_mixed_width(system):
    """Test 4: Reconfigure rp_wide to alternate RM."""
    print("\n--- Test 4: Reconfigure rp_wide to mixed_width_alt_rm ---")

    system.reconfigure('rp_wide', 'mixed_width_alt_rm')
    time.sleep(0.3)

    api = system.get_rm_api('rp_wide')

    # Alt RM inverts everything
    api.write_enable(1)
    api.write_byte_in(0x55)
    api.write_half_in(0xAAAA)
    api.write_word_in(0x12345678)

    for _ in range(5):
        time.sleep(0.005)
        api.read_flag_out()

    flag = api.read_flag_out()
    byte_out = api.read_status_byte()
    half_out = api.read_result_half()
    word_out = api.read_result_word()

    print(f"    flag_out    = {flag} (expected 0 = ~1)")
    print(f"    status_byte = 0x{byte_out:02X} (expected 0xAA = ~0x55)")
    print(f"    result_half = 0x{half_out:04X} (expected 0x5555 = ~0xAAAA)")
    print(f"    result_word = 0x{word_out:08X} (expected 0xEDCBA987 = ~0x12345678)")

    check(flag == 0, "alt RM: flag_out = ~enable = 0")
    check(byte_out == 0xAA, "alt RM: ~0x55 = 0xAA")
    check(half_out == 0x5555, "alt RM: ~0xAAAA = 0x5555")
    check(word_out == 0xEDCBA987, "alt RM: ~0x12345678 = 0xEDCBA987")


def test_reconfig_many_ports(system):
    """Test 5: Reconfigure rp_many to alternate RM."""
    print("\n--- Test 5: Reconfigure rp_many to many_port_alt_rm ---")

    system.reconfigure('rp_many', 'many_port_alt_rm')
    time.sleep(0.3)

    api = system.get_rm_api('rp_many')

    api.write_in_0(1000)
    api.write_in_1(400)
    api.write_in_2(300)
    api.write_in_3(100)

    for _ in range(5):
        time.sleep(0.005)
        api.read_out_0()

    out_0 = api.read_out_0()  # in_0 - in_1 = 600
    out_1 = api.read_out_1()  # in_2 - in_3 = 200
    # out_2 = acc (nondeterministic)
    out_3 = api.read_out_3()  # in_0 + in_1 + in_2 + in_3 = 1800

    print(f"    out_0 = {out_0} (expected 600 = 1000-400)")
    print(f"    out_1 = {out_1} (expected 200 = 300-100)")
    print(f"    out_3 = {out_3} (expected 1800 = sum)")

    check(out_0 == 600, "alt many: out_0 = in_0 - in_1")
    check(out_1 == 200, "alt many: out_1 = in_2 - in_3")
    check(out_3 == 1800, "alt many: out_3 = sum of all inputs")


def test_rapid_reconfig(system):
    """Test 6: Rapid back-and-forth reconfiguration."""
    print("\n--- Test 6: Rapid reconfiguration (5 swaps) ---")

    for i in range(5):
        rm_name = 'mixed_width_rm' if i % 2 == 0 else 'mixed_width_alt_rm'
        system.reconfigure('rp_wide', rm_name)
        time.sleep(0.2)

        api = system.get_rm_api('rp_wide')
        api.write_enable(1)
        api.write_byte_in(42)

        for _ in range(3):
            time.sleep(0.005)
            api.read_status_byte()

        byte_out = api.read_status_byte()

        if i % 2 == 0:
            expected = 43  # 42 + 1
        else:
            expected = 0xFF - 42  # ~42 = 213

        check(byte_out == expected, f"swap {i+1}: {rm_name} -> byte=0x{byte_out:02X} (expected 0x{expected:02X})")

    print(f"    Completed 5 rapid reconfigurations successfully")


def test_static_continuity(system):
    """Test 7: Static region keeps running during reconfigs."""
    print("\n--- Test 7: Static region continuity ---")

    static_api = system.get_static_api()
    before = static_api.read_tick_counter()

    system.reconfigure('rp_wide', 'mixed_width_rm')
    time.sleep(0.2)

    after = static_api.read_tick_counter()
    delta = after - before

    print(f"    tick_counter: {before:,} -> {after:,} (delta={delta:,})")
    check(delta > 1000, f"Static region ran during reconfig: +{delta:,} cycles")


def main():
    print("=" * 60)
    print("Stress Test: Width-Aware DPI System Limits")
    print("=" * 60)

    with PRSystem(config=str(THIS_DIR / 'pr_config.yaml')) as system:
        print("\n[Build] Building static region + 4 RMs...")
        system.build()
        print(f"    Partitions: {list(system.partitions.keys())}")
        print(f"    RMs: {list(system.modules.keys())}")

        print("\n[Sim] Starting simulation...")
        system.simulate()

        print("[Load] Loading initial RMs...")
        system.load('rp_wide', 'mixed_width_rm')
        system.load('rp_many', 'many_port_rm')
        time.sleep(0.3)

        test_mixed_width_basic(system)
        test_mixed_width_zero(system)
        test_many_ports_basic(system)
        test_reconfig_mixed_width(system)
        test_reconfig_many_ports(system)
        test_rapid_reconfig(system)
        test_static_continuity(system)

    print("\n" + "=" * 60)
    print(f"Results: {PASS} passed, {FAIL} failed")
    print("=" * 60)
    return 1 if FAIL > 0 else 0


if __name__ == '__main__':
    sys.exit(main())
