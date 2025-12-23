#!/usr/bin/env python3
"""
Test suite to verify cycle accuracy with barrier synchronization.

With cycle_accurate=True, all Verilator processes (static region + RMs) synchronize
at each clock cycle via a shared memory barrier. This ensures cycle-accurate simulation.

Key tests:
1. Latency Variance - Measures simulation-internal latency. MUST be 0 for cycle accuracy.
2. Counter Correlation - Verifies counters stay in sync. MUST be 1.0 for cycle accuracy.
3. Echo Consistency - Measures Python read timing (INFO only, expected to vary).

Note: Tests 1 and 2 verify simulation cycle accuracy. Test 3 measures Python-to-simulation
timing which is async by design and does NOT indicate cycle inaccuracy.
"""

import sys
import time
import statistics
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from partial_reconfiguration import PRSystem


def tprint(msg: str):
    """Timestamped print for debugging."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


def run_test_latency_variance(echo_api, static_api):
    """
    Test 1: Latency Variance Detection

    The RM calculates latency_measurement = rm_counter - static_counter_in

    In a cycle-accurate system:
    - Both counters increment at the same rate (same clock)
    - The difference should be CONSTANT (equal to pipeline delay)

    With queue latency:
    - Queue introduces variable delay
    - The difference VARIES over time
    - Variance > 0 proves cycle inaccuracy
    """
    tprint("")
    tprint("=" * 70)
    tprint("TEST 1: Latency Variance Detection")
    tprint("=" * 70)

    # Collect latency measurements over time
    latency_samples = []
    rm_counter_samples = []
    static_counter_samples = []
    echo_samples = []

    NUM_SAMPLES = 200
    WARMUP_SAMPLES = 10
    SAMPLE_INTERVAL = 0.005  # 5ms between samples

    tprint(f"Collecting {NUM_SAMPLES} samples at {SAMPLE_INTERVAL*1000}ms intervals...")
    tprint(f"(skipping first {WARMUP_SAMPLES} samples for warm-up)")

    for i in range(NUM_SAMPLES):
        # Read all values as close together as possible
        latency = int(echo_api.read_latency_measurement())
        rm_cnt = int(echo_api.read_rm_counter())
        static_cnt = int(static_api.read_activity_counter())
        echo_cnt = int(echo_api.read_static_counter_echo())

        if i >= WARMUP_SAMPLES:
            latency_samples.append(latency)
            rm_counter_samples.append(rm_cnt)
            static_counter_samples.append(static_cnt)
            echo_samples.append(echo_cnt)

        time.sleep(SAMPLE_INTERVAL)

    # Analyze results
    tprint("")
    tprint("RESULTS:")
    tprint("-" * 50)

    # Latency variance analysis
    latency_mean = statistics.mean(latency_samples)
    latency_variance = statistics.variance(latency_samples)
    latency_stdev = statistics.stdev(latency_samples)
    latency_min = min(latency_samples)
    latency_max = max(latency_samples)
    latency_range = latency_max - latency_min

    tprint(f"Latency Measurement (rm_counter - static_counter_in):")
    tprint(f"  Mean:     {latency_mean:,.2f} cycles")
    tprint(f"  Variance: {latency_variance:,.2f}")
    tprint(f"  StdDev:   {latency_stdev:,.2f} cycles")
    tprint(f"  Min:      {latency_min:,} cycles")
    tprint(f"  Max:      {latency_max:,} cycles")
    tprint(f"  Range:    {latency_range:,} cycles")

    # Calculate Python-side delta (static_counter - echo)
    # This measures round-trip: static sends counter, RM echoes it back
    round_trip_deltas = [
        static_counter_samples[i] - echo_samples[i]
        for i in range(len(static_counter_samples))
    ]
    rt_mean = statistics.mean(round_trip_deltas)
    rt_variance = statistics.variance(round_trip_deltas)
    rt_min = min(round_trip_deltas)
    rt_max = max(round_trip_deltas)

    tprint("")
    tprint(f"Round-Trip Delta (static_counter - echo):")
    tprint(f"  Mean:     {rt_mean:,.2f} cycles")
    tprint(f"  Variance: {rt_variance:,.2f}")
    tprint(f"  Min:      {rt_min:,} cycles")
    tprint(f"  Max:      {rt_max:,} cycles")
    tprint(f"  Range:    {rt_max - rt_min:,} cycles")

    # Counter progression check
    num_samples = len(rm_counter_samples)
    rm_progression = [
        rm_counter_samples[i+1] - rm_counter_samples[i]
        for i in range(num_samples - 1)
    ]
    static_progression = [
        static_counter_samples[i+1] - static_counter_samples[i]
        for i in range(num_samples - 1)
    ]

    rm_prog_variance = statistics.variance(rm_progression)
    static_prog_variance = statistics.variance(static_progression)

    tprint("")
    tprint(f"Counter Progression (cycles between samples):")
    tprint(f"  RM counter variance:     {rm_prog_variance:,.2f}")
    tprint(f"  Static counter variance: {static_prog_variance:,.2f}")

    # Print sample data
    tprint("")
    tprint("Sample data (first 10):")
    tprint(f"{'Sample':<8} {'Static':<12} {'Echo':<12} {'RM':<12} {'Latency':<12} {'RT Delta':<12}")
    tprint("-" * 70)
    for i in range(min(10, num_samples)):
        rt_delta = static_counter_samples[i] - echo_samples[i]
        tprint(f"{i:<8} {static_counter_samples[i]:<12,} {echo_samples[i]:<12,} "
               f"{rm_counter_samples[i]:<12,} {latency_samples[i]:<12,} {rt_delta:<12,}")

    # ASSERTIONS - These should FAIL to prove cycle inaccuracy
    tprint("")
    tprint("=" * 70)
    tprint("CYCLE ACCURACY ASSERTIONS:")
    tprint("=" * 70)

    cycle_accurate = True

    # Assertion 1: Latency should be constant (variance = 0)
    if latency_variance > 0:
        tprint(f"[FAIL] Latency variance is {latency_variance:.2f}, expected 0")
        tprint("       -> PROVES: Queue introduces variable delay")
        cycle_accurate = False
    else:
        tprint(f"[PASS] Latency variance is 0")

    # Assertion 2: Latency range should be 0
    if latency_range > 0:
        tprint(f"[FAIL] Latency range is {latency_range}, expected 0")
        tprint("       -> PROVES: Latency varies between samples")
        cycle_accurate = False
    else:
        tprint(f"[PASS] Latency range is 0")

    # Assertion 3: Round-trip delta (Python side)
    # NOTE: Variance is expected here because Python reads are not atomic.
    # The simulation advances between the two read() calls.
    # This is NOT a failure of cycle accuracy - it's an artifact of async Python reads.
    if rt_variance > 0:
        tprint(f"[INFO] Round-trip variance is {rt_variance:.2f}")
        tprint("       (Expected: Python reads are not synchronized with simulation)")
    else:
        tprint(f"[PASS] Round-trip variance is 0")

    # Assertion 4: In true cycle-accurate, RM counter should equal static counter
    # (or differ by exactly the pipeline depth, which should be ~2-3 cycles)
    # A large mean latency indicates significant queue buffering
    EXPECTED_PIPELINE_DEPTH = 3  # Reasonable for direct connection
    if abs(latency_mean) > EXPECTED_PIPELINE_DEPTH * 10:
        tprint(f"[FAIL] Mean latency is {latency_mean:.0f} cycles, expected ~{EXPECTED_PIPELINE_DEPTH}")
        tprint("       -> PROVES: Queue adds significant buffering delay")
        cycle_accurate = False
    else:
        tprint(f"[PASS] Mean latency is within expected range")

    tprint("")
    if cycle_accurate:
        tprint("CONCLUSION: Simulation appears cycle-accurate")
    else:
        tprint("CONCLUSION: CYCLE INACCURACY DETECTED")
        tprint("")
        tprint("The static region and RM run as independent processes.")
        tprint("Switchboard queues introduce variable latency that breaks")
        tprint("cycle-level synchronization between the two domains.")

    return cycle_accurate


def run_test_counter_correlation(echo_api, static_api):
    """
    Test 2: Counter Correlation Analysis

    If cycle-accurate, the RM's counter and static's counter should be perfectly
    correlated (linear relationship with R^2 = 1.0).

    With queue latency, the correlation will be imperfect because the RM's view
    of the static counter is delayed by variable amounts.
    """
    tprint("")
    tprint("=" * 70)
    tprint("TEST 2: Counter Correlation Analysis")
    tprint("=" * 70)

    # Collect paired samples
    pairs = []
    NUM_SAMPLES = 100

    tprint(f"Collecting {NUM_SAMPLES} counter pairs...")

    for _ in range(NUM_SAMPLES):
        static_cnt = int(static_api.read_activity_counter())
        rm_cnt = int(echo_api.read_rm_counter())
        pairs.append((static_cnt, rm_cnt))
        time.sleep(0.002)

    # Calculate correlation coefficient
    static_vals = [p[0] for p in pairs]
    rm_vals = [p[1] for p in pairs]

    mean_s = statistics.mean(static_vals)
    mean_r = statistics.mean(rm_vals)

    # Pearson correlation
    numerator = sum((s - mean_s) * (r - mean_r) for s, r in pairs)
    denom_s = sum((s - mean_s) ** 2 for s in static_vals) ** 0.5
    denom_r = sum((r - mean_r) ** 2 for r in rm_vals) ** 0.5

    if denom_s > 0 and denom_r > 0:
        correlation = numerator / (denom_s * denom_r)
    else:
        correlation = 0

    r_squared = correlation ** 2

    tprint(f"Pearson correlation: {correlation:.6f}")
    tprint(f"R^2 coefficient:     {r_squared:.6f}")

    # Check if correlation is perfect
    if r_squared < 0.9999:
        tprint(f"[FAIL] R^2 is {r_squared:.6f}, expected 1.0 for cycle-accurate")
        tprint("       -> PROVES: Counters drift due to async queue communication")
        return False
    else:
        tprint(f"[PASS] R^2 is near 1.0")
        return True


def run_test_echo_consistency(echo_api, static_api):
    """
    Test 3: Echo Consistency Check (Informational)

    This test measures the delay between Python reads of static_counter and echo.
    Because Python reads are ASYNC (not synchronized with simulation), variance
    is EXPECTED and does NOT indicate cycle inaccuracy.

    This test is informational only - it always "passes" because Python async
    reads are a separate concern from simulation cycle accuracy.
    """
    tprint("")
    tprint("=" * 70)
    tprint("TEST 3: Echo Consistency Check (Python Async Reads)")
    tprint("=" * 70)

    delays = []
    NUM_SAMPLES = 100

    tprint(f"Measuring Python read timing over {NUM_SAMPLES} samples...")

    for _ in range(NUM_SAMPLES):
        static_cnt = int(static_api.read_activity_counter())
        echo_cnt = int(echo_api.read_static_counter_echo())
        delay = static_cnt - echo_cnt
        delays.append(delay)
        time.sleep(0.002)

    delay_mean = statistics.mean(delays)
    delay_variance = statistics.variance(delays)
    delay_min = min(delays)
    delay_max = max(delays)

    tprint(f"Python read timing statistics:")
    tprint(f"  Mean:     {delay_mean:,.2f} cycles")
    tprint(f"  Variance: {delay_variance:,.2f}")
    tprint(f"  Min:      {delay_min:,} cycles")
    tprint(f"  Max:      {delay_max:,} cycles")
    tprint(f"  Range:    {delay_max - delay_min:,} cycles")

    # This is informational - variance is EXPECTED because Python reads are async
    tprint(f"[INFO] Python read variance is {delay_variance:.2f}")
    tprint("       (Expected: Python reads are not synchronized with simulation)")
    tprint("       This does NOT affect simulation cycle accuracy.")

    # Always return True - this is informational only
    return True


def main():
    """Run all cycle accuracy tests."""
    tprint("=" * 70)
    tprint("CYCLE ACCURACY TEST SUITE")
    tprint("=" * 70)
    tprint("")
    tprint("This test suite verifies cycle accuracy with barrier synchronization.")
    tprint("Using cycle_accurate=True for barrier-synchronized simulation.")
    tprint("")
    tprint("Expected result: ALL TESTS SHOULD PASS")
    tprint("Passing proves that barrier sync provides cycle-accurate simulation.")
    tprint("")

    # BUILD ONCE
    tprint("=" * 70)
    tprint("BUILDING SIMULATION (one-time)")
    tprint("=" * 70)

    with PRSystem(config='pr_config.yaml', cycle_accurate=True) as system:
        system.build()
        system.simulate()
        # echo_counter_rm is already loaded as initial_rm for rp_echo

        time.sleep(0.2)  # Allow simulation to stabilize

        # Get APIs
        echo_api = system.get_rm_api('rp_echo')
        static_api = system.get_static_api()

        tprint("")
        tprint("Build complete. Running all tests...")

        # RUN ALL TESTS
        results = []
        results.append(("Latency Variance", run_test_latency_variance(echo_api, static_api)))
        results.append(("Counter Correlation", run_test_counter_correlation(echo_api, static_api)))
        results.append(("Echo Consistency", run_test_echo_consistency(echo_api, static_api)))

        # Summary
        tprint("")
        tprint("=" * 70)
        tprint("FINAL SUMMARY")
        tprint("=" * 70)

        # Check critical tests (1 and 2 determine cycle accuracy)
        latency_passed = results[0][1]  # Latency Variance
        correlation_passed = results[1][1]  # Counter Correlation

        for name, passed in results:
            if name == "Echo Consistency":
                tprint(f"  {name}: INFO (Python async reads)")
            else:
                status = "PASS" if passed else "FAIL"
                tprint(f"  {name}: {status}")

        tprint("")
        if latency_passed and correlation_passed:
            tprint("CYCLE-ACCURATE SIMULATION CONFIRMED")
            tprint("")
            tprint("Barrier synchronization successfully provides cycle-accurate")
            tprint("simulation between all Verilator processes.")
            tprint("")
            tprint("Note: Python reads are async by design and do not affect")
            tprint("the cycle accuracy of the simulation itself.")
            return 0
        else:
            tprint("CYCLE INACCURACY DETECTED")
            tprint("")
            if not latency_passed:
                tprint("- Latency variance > 0 indicates variable queue delay")
            if not correlation_passed:
                tprint("- Counter correlation < 1.0 indicates counter drift")
            return 1


if __name__ == "__main__":
    sys.exit(main())
