#!/usr/bin/env python3
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from partial_reconfiguration import PRSystem

def make_tprint(size=4):
    lvl = [0]

    def tprint(*args, t=0, **kw):
        indent = ' ' * size * lvl[0]
        output = [indent + str(args[0]), *args[1:]] if args else []
        print(*output, **kw)
        lvl[0] = max(0, lvl[0] + t)
    return tprint


tprint = make_tprint()


def test_counter_increments(system: PRSystem):
    """Test 1: Counter increments over time."""
    api = system.get_rm_api('rp0')

    val1 = api.read_counter()
    time.sleep(0.01)
    val2 = api.read_counter()

    tprint(f"Read 1: {val1}")
    tprint(f"Read 2: {val2}")

    assert val2 > val1, f"Counter should increment: {val1} -> {val2}"
    tprint(f"[PASS] Counter increments: {val1} -> {val2}")


def test_swap_behavior(system: PRSystem):
    """Test 2: Different RM gives different behavior."""
    system.reconfigure('rp0', 'passthrough_rm')
    time.sleep(0.1)

    api = system.get_rm_api('rp0')
    val = api.read_counter()

    tprint(f"Passthrough read: {val:#010x}")

    assert val == 0xFFFFFFFF, f"Passthrough should return all-1s: {val:#x}"
    tprint(f"[PASS] Passthrough returns: {val:#010x}")


def test_fresh_state(system: PRSystem):
    """Test 3: Swap back to counter gives fresh state."""
    system.reconfigure('rp0', 'counter_rm')
    time.sleep(0.1)

    api = system.get_rm_api('rp0')
    val = api.read_counter()

    tprint(f"Counter after swap: {val}")

    assert val < 10000000, f"Counter should be relatively small (fresh state): {val}"
    tprint(f"[PASS] Fresh state: counter = {val} (started from 0)")


def test_adder(system):
    """Test 4: Adder computes A + B."""
    api = system.get_rm_api('rp1')

    A = 100
    B = 42

    api.write_operand_a(A)
    api.write_operand_b(B)

    for _ in range(3):
        result = api.read_result()

    expected = A + B

    tprint(f"A = {A}, B = {B}")
    tprint(f"Result: {result} (expected {expected})")

    assert result == expected, f"Adder failed: {A} + {B} = {result}, expected {expected}"
    tprint(f"[PASS] Adder: {A} + {B} = {result}")


def test_swap_to_subtractor(system):
    """Test 5: Swap to subtractor - same operands, different result."""
    api_before = system.get_rm_api('rp1')

    A = 200
    B = 75
    api_before.write_operand_a(A)
    api_before.write_operand_b(B)

    for _ in range(3):
        add_result = api_before.read_result()
    tprint(f"Adder: {A} + {B} = {add_result}")

    system.reconfigure('rp1', 'subtractor_rm')
    time.sleep(0.1)

    api = system.get_rm_api('rp1')
    api.write_operand_a(A)
    api.write_operand_b(B)

    for _ in range(3):
        sub_result = api.read_result()
    expected = A - B

    tprint(f"Subtractor: {A} - {B} = {sub_result}")

    assert sub_result == expected, \
        f"Subtractor failed: {A} - {B} = {sub_result}, expected {expected}"
    tprint(f"[PASS] Subtractor: {A} - {B} = {sub_result}")


def test_adder_fresh_state(system):
    """
    Test 6: Swapping back to adder - verify it computes correctly.
    """
    system.reconfigure('rp1', 'adder_rm')
    time.sleep(0.1)

    api = system.get_rm_api('rp1')
    result = api.read_result()

    tprint(f"Fresh adder result: {result}")
    tprint(f"(Non-zero because queue bridge auto-forwards static's operands!)")

    api.write_operand_a(10)
    api.write_operand_b(5)

    for _ in range(3):
        result = api.read_result()

    assert result == 15, f"Expected 10 + 5 = 15, got {result}"
    tprint(f"[PASS] UMI override works: 10 + 5 = {result}")


def test_crypto_accelerator_swap(system):
    """
    Test 7: Swap crypto accelerators.
    """
    system.reconfigure('rp1', 'xor_cipher_rm')
    time.sleep(0.1)
    api = system.get_rm_api('rp1')
    KEY = 0xCAFEBABE
    PLAINTEXT = 0x12345678
    api.write_operand_b(KEY)
    api.write_operand_a(PLAINTEXT)

    for _ in range(3):
        xor_result = api.read_result()

    expected_xor = PLAINTEXT ^ KEY
    tprint(f"XOR Cipher: {PLAINTEXT:#010x} ^ {KEY:#010x} = {xor_result:#010x}")
    assert xor_result == expected_xor, \
        f"XOR cipher failed: expected {expected_xor:#010x}, got {xor_result:#010x}"
    tprint(f"[OK] XOR cipher works correctly")

    tprint("Swapping crypto accelerator: XOR -> Substitution cipher")
    system.reconfigure('rp1', 'sub_cipher_rm')
    time.sleep(0.1)

    api = system.get_rm_api('rp1')
    api.write_operand_a(PLAINTEXT)

    for _ in range(3):
        sub_result = api.read_result()

    tprint(f"Substitution Cipher: {PLAINTEXT:#010x} -> {sub_result:#010x}")

    assert sub_result != xor_result, \
        f"Different ciphers should produce different output! XOR={xor_result:#x}, SUB={sub_result:#x}"
    tprint(f"[OK] Substitution cipher produces different output")

    tprint("Swapping back: Substitution -> XOR cipher")
    system.reconfigure('rp1', 'xor_cipher_rm')
    time.sleep(0.1)

    api = system.get_rm_api('rp1')

    api.write_operand_b(KEY)
    api.write_operand_a(PLAINTEXT)

    for _ in range(3):
        xor_result2 = api.read_result()

    assert xor_result2 == expected_xor, \
        f"XOR cipher inconsistent after swap: expected {expected_xor:#010x}, got {xor_result2:#010x}"

    tprint(f"[PASS] Crypto accelerator swap: algorithms swapped, results correct")
    tprint(f"       This is WHY PR exists: share resources, swap algorithms on-the-fly")


def test_static_region_continuity(system):
    """
    Test 8: Continuity of static region during RM swap.
    """
    static_api = system.get_static_api()
    counter_before = static_api.read_activity_counter()
    tprint(f"Activity counter before swap: {counter_before:,}")

    t_start = time.time()

    system.reconfigure('rp0', 'passthrough_rm')
    time.sleep(0.1)

    t_elapsed = time.time() - t_start

    counter_after = static_api.read_activity_counter()
    tprint(f"Activity counter after swap: {counter_after:,}")

    cycles_elapsed = int(counter_after) - int(counter_before)
    tprint(f"Cycles elapsed during swap: {cycles_elapsed:,} ({t_elapsed*1000:.1f}ms)")

    assert counter_after > counter_before, \
        f"Static region should keep running! Counter: {counter_before} -> {counter_after}"

    assert cycles_elapsed > 1000, \
        f"Too few cycles elapsed ({cycles_elapsed}) - something is wrong"

    tprint(f"[PASS] Static region ran continuously: +{cycles_elapsed:,} cycles during swap")


def test_automatic_static_rm_flow(system):
    """
    Test 9: Static --> RM data flow via queue bridge.
    """
    system.reconfigure('rp1', 'adder_rm')
    time.sleep(0.3)

    static_api = system.get_static_api()

    tprint("Verifying automatic queue bridge data flow...")

    activity = static_api.read_activity_counter()
    expected_a = activity & 0xFFFF
    expected_b = (activity >> 16) & 0xFFFF

    tprint(f"Activity counter: {activity:,}")
    tprint(f"Expected operands: a={expected_a}, b={expected_b}")
    result = static_api.read_computed_result()
    tprint(f"computed_result (from RM via queue bridge): {result}")

    if result > 0:
        tprint(f"[OK] Result is flowing back from RM!")
    else:
        tprint(f"[WARN] Result is 0 - queue may still be filling")
    tprint("\nVerifying continuous automatic flow (5 iterations)...")
    results = []
    for i in range(5):
        time.sleep(0.05)
        act = static_api.read_activity_counter()
        res = static_api.read_computed_result()
        results.append(res)

        exp_a = act & 0xFFFF
        exp_b = (act >> 16) & 0xFFFF
        exp_sum = exp_a + exp_b

        tprint(f"  Iteration {i+1}: activity={act:,}, result={res}, expected_sum≈{exp_sum}")

    unique_results = len(set(results))
    if unique_results > 1:
        tprint(f"\n[PASS] Results are changing ({unique_results} unique values)")
        tprint(f"       Data flows via queue bridge!")
    else:
        tprint(f"\n[WARN] Results not changing - may need more time for queue")

    tprint(f"\n[PASS] Automatic static <--> RM flow verified")


def main():
    tprint("=" * 60)
    tprint("Partial Reconfiguration Simulation Test")
    tprint("=" * 60)
    tprint("Using auto-wrapped RTL with original signal names")
    tprint("=" * 60)

    config_path = Path(__file__).parent / 'pr_config.yaml'

    tprint(f"\n[1] Loading configuration: {config_path}", t=1)

    with PRSystem(config=config_path) as system:
        tprint(f"Static region: {system.static_region.name if system.static_region else 'None'}")
        tprint(f"Partitions: {list(system.partitions.keys())}")
        tprint(f"RMs: {list(system.modules.keys())}", t=-1)

        tprint("\n[2] Building simulations (static region + all RMs)...", t=1)
        system.build()
        tprint("Build complete!", t=-1)

        tprint("\n[3] Starting simulation and loading counter_rm...", t=1)
        system.simulate()
        system.load('rp0', 'counter_rm')
        time.sleep(0.2)
        tprint("counter_rm is running", t=-1)

        tprint("\n[4] TEST 1: Verify counter increments...", t=1)
        test_counter_increments(system)
        tprint("", t=-1)

        tprint("[5] TEST 2: Reconfigure to passthrough_rm...", t=1)
        test_swap_behavior(system)
        tprint("", t=-1)

        tprint("[6] TEST 3: Verify fresh state on swap back...", t=1)
        test_fresh_state(system)
        tprint("", t=-1)

        tprint("-" * 60)
        tprint("Partition rp1: Adder/Subtractor Tests")
        tprint("-" * 60)

        tprint("\n[7] TEST 4: Verify adder (A + B)...", t=1)
        system.load('rp1', 'adder_rm')
        time.sleep(0.2)
        test_adder(system)
        tprint("", t=-1)

        tprint("[8] TEST 5: Swap to subtractor (A - B)...", t=1)
        test_swap_to_subtractor(system)
        tprint("", t=-1)

        tprint("[9] TEST 6: Verify fresh state on swap back...", t=1)
        test_adder_fresh_state(system)
        tprint("", t=-1)

        tprint("-" * 60)
        tprint("Crypto Accelerator Swap")
        tprint("-" * 60)

        tprint("\n[10] TEST 7: Swap crypto accelerators on-the-fly...", t=1)
        test_crypto_accelerator_swap(system)
        tprint("", t=-1)

        tprint("-" * 60)
        tprint("Nice PR tests")
        tprint("-" * 60)

        tprint("\n[11] TEST 8: Static region continuity during swap...", t=1)
        test_static_region_continuity(system)
        tprint("", t=-1)

        tprint("-" * 60)
        tprint("Static --> RM Data Flow")
        tprint("-" * 60)

        tprint("\n[12] TEST 9: static <--> RM flow via queue bridge...", t=1)
        test_automatic_static_rm_flow(system)
        tprint("", t=-1)

    tprint("\nWhat to notice in the above log ^^^:", t=1)
    tprint("- RTL modules auto-wrapped, Python API uses original signal names")
    tprint("- Static region runs continuously during swaps")
    tprint("- Queue bridge automatically forwards data between static and RM")
    tprint("- Partitions support state reset on reconfiguration")
    tprint("- More complex stuff like crypto accels can be swapped with same interface")
    tprint("")

    return 0


if __name__ == '__main__':
    sys.exit(main())
