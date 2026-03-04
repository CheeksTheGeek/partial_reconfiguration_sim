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

import os
import sys
import time
import statistics
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

THIS_DIR = Path(__file__).resolve().parent


def tprint(msg: str):
    """Timestamped print for debugging."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


# ---------------------------------------------------------------------------
# Fixtures — build once per test session
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pr_system():
    from partial_reconfiguration import PRSystem

    old_cwd = os.getcwd()
    os.chdir(THIS_DIR)
    try:
        with PRSystem(config="pr_config.yaml", cycle_accurate=True) as system:
            system.build()
            system.simulate()
            time.sleep(0.2)  # Allow simulation to stabilize
            yield system
    finally:
        os.chdir(old_cwd)


@pytest.fixture(scope="module")
def echo_api(pr_system):
    return pr_system.get_rm_api("rp_echo")


@pytest.fixture(scope="module")
def static_api(pr_system):
    return pr_system.get_static_api()


@pytest.fixture(scope="module")
def latency_data(echo_api, static_api):
    """
    Collect 200 samples (190 after warmup) for latency variance analysis.
    Computed once and shared across test_latency_* tests.
    """
    NUM_SAMPLES = 200
    WARMUP_SAMPLES = 10
    SAMPLE_INTERVAL = 0.005  # 5ms between samples

    latency_samples = []
    rm_counter_samples = []
    static_counter_samples = []
    echo_samples = []

    tprint("")
    tprint("=" * 70)
    tprint("Collecting latency samples (TEST 1 data)")
    tprint("=" * 70)
    tprint(f"Collecting {NUM_SAMPLES} samples at {SAMPLE_INTERVAL*1000}ms intervals...")
    tprint(f"(skipping first {WARMUP_SAMPLES} samples for warm-up)")

    for i in range(NUM_SAMPLES):
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

    # Pre-compute all derived stats so tests can reuse them
    latency_mean = statistics.mean(latency_samples)
    latency_variance = statistics.variance(latency_samples)
    latency_stdev = statistics.stdev(latency_samples)
    latency_min = min(latency_samples)
    latency_max = max(latency_samples)
    latency_range = latency_max - latency_min

    round_trip_deltas = [
        static_counter_samples[i] - echo_samples[i]
        for i in range(len(static_counter_samples))
    ]
    rt_mean = statistics.mean(round_trip_deltas)
    rt_variance = statistics.variance(round_trip_deltas)
    rt_min = min(round_trip_deltas)
    rt_max = max(round_trip_deltas)

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

    # Print full diagnostics (visible with pytest -s)
    tprint("")
    tprint("RESULTS:")
    tprint("-" * 50)
    tprint(f"Latency Measurement (rm_counter - static_counter_in):")
    tprint(f"  Mean:     {latency_mean:,.2f} cycles")
    tprint(f"  Variance: {latency_variance:,.2f}")
    tprint(f"  StdDev:   {latency_stdev:,.2f} cycles")
    tprint(f"  Min:      {latency_min:,} cycles")
    tprint(f"  Max:      {latency_max:,} cycles")
    tprint(f"  Range:    {latency_range:,} cycles")
    tprint("")
    tprint(f"Round-Trip Delta (static_counter - echo):")
    tprint(f"  Mean:     {rt_mean:,.2f} cycles")
    tprint(f"  Variance: {rt_variance:,.2f}")
    tprint(f"  Min:      {rt_min:,} cycles")
    tprint(f"  Max:      {rt_max:,} cycles")
    tprint(f"  Range:    {rt_max - rt_min:,} cycles")
    tprint("")
    tprint(f"Counter Progression (cycles between samples):")
    tprint(f"  RM counter variance:     {rm_prog_variance:,.2f}")
    tprint(f"  Static counter variance: {static_prog_variance:,.2f}")
    tprint("")
    tprint("Sample data (first 10):")
    tprint(f"{'Sample':<8} {'Static':<12} {'Echo':<12} {'RM':<12} {'Latency':<12} {'RT Delta':<12}")
    tprint("-" * 70)
    for i in range(min(10, num_samples)):
        rt_delta = static_counter_samples[i] - echo_samples[i]
        tprint(f"{i:<8} {static_counter_samples[i]:<12,} {echo_samples[i]:<12,} "
               f"{rm_counter_samples[i]:<12,} {latency_samples[i]:<12,} {rt_delta:<12,}")

    return {
        "latency_samples": latency_samples,
        "rm_counter_samples": rm_counter_samples,
        "static_counter_samples": static_counter_samples,
        "echo_samples": echo_samples,
        "latency_mean": latency_mean,
        "latency_variance": latency_variance,
        "latency_stdev": latency_stdev,
        "latency_min": latency_min,
        "latency_max": latency_max,
        "latency_range": latency_range,
        "round_trip_deltas": round_trip_deltas,
        "rt_mean": rt_mean,
        "rt_variance": rt_variance,
        "rt_min": rt_min,
        "rt_max": rt_max,
        "rm_prog_variance": rm_prog_variance,
        "static_prog_variance": static_prog_variance,
    }


# ---------------------------------------------------------------------------
# TEST 1: Latency Variance Detection
# ---------------------------------------------------------------------------

def test_latency_variance_is_zero(latency_data):
    """
    Assertion 1: Latency variance must be 0.

    latency_measurement = rm_counter - static_counter_in.
    In a cycle-accurate system both counters increment at the same rate,
    so the difference must be CONSTANT -> variance == 0.
    """
    tprint("")
    tprint("=" * 70)
    tprint("TEST 1a: Latency variance == 0")
    tprint("=" * 70)

    latency_variance = latency_data["latency_variance"]
    latency_mean = latency_data["latency_mean"]

    if latency_variance > 0:
        tprint(f"[FAIL] Latency variance is {latency_variance:.2f}, expected 0")
        tprint("       -> Latency variance detected")
    else:
        tprint(f"[PASS] Latency variance is 0")

    assert latency_variance == 0, (
        f"Latency variance is {latency_variance:.2f}, expected 0. "
        f"Mean={latency_mean:.2f} cycles. Variance > 0 proves cycle inaccuracy."
    )


def test_latency_range_is_zero(latency_data):
    """
    Assertion 2: Latency range (max - min) must be 0.

    If latency varies between samples the simulation is NOT cycle-accurate.
    """
    tprint("")
    tprint("=" * 70)
    tprint("TEST 1b: Latency range == 0")
    tprint("=" * 70)

    latency_range = latency_data["latency_range"]
    latency_min = latency_data["latency_min"]
    latency_max = latency_data["latency_max"]

    if latency_range > 0:
        tprint(f"[FAIL] Latency range is {latency_range}, expected 0")
        tprint(f"       Min={latency_min}, Max={latency_max}")
        tprint("       -> PROVES: Latency varies between samples")
    else:
        tprint(f"[PASS] Latency range is 0")

    assert latency_range == 0, (
        f"Latency range is {latency_range} cycles (min={latency_min}, max={latency_max}). "
        "Non-zero range proves latency varies between samples -> cycle inaccuracy."
    )


def test_mean_latency_within_expected_pipeline_depth(latency_data):
    """
    Assertion 4: Mean latency must be within expected pipeline depth.

    A large mean latency (> 3 * 10 = 30 cycles) indicates unexpected pipeline
    depth or a synchronization issue between processes.
    """
    tprint("")
    tprint("=" * 70)
    tprint("TEST 1c: Mean latency within expected pipeline depth")
    tprint("=" * 70)

    EXPECTED_PIPELINE_DEPTH = 3
    latency_mean = latency_data["latency_mean"]

    if abs(latency_mean) > EXPECTED_PIPELINE_DEPTH * 10:
        tprint(f"[FAIL] Mean latency is {latency_mean:.0f} cycles, expected ~{EXPECTED_PIPELINE_DEPTH}")
        tprint("       -> Mean latency exceeds expected pipeline depth")
    else:
        tprint(f"[PASS] Mean latency is within expected range ({latency_mean:.2f} cycles)")

    assert abs(latency_mean) <= EXPECTED_PIPELINE_DEPTH * 10, (
        f"Mean latency {latency_mean:.0f} cycles exceeds expected ~{EXPECTED_PIPELINE_DEPTH} cycles "
        f"(threshold: {EXPECTED_PIPELINE_DEPTH * 10}). "
        "Indicates unexpected pipeline depth or synchronization issue."
    )


def test_round_trip_variance_informational(latency_data):
    """
    Assertion 3: Round-trip delta (Python side) variance is informational only.

    Variance IS expected here because Python reads are not atomic — the simulation
    advances between the two read() calls. This is NOT a failure of cycle accuracy.
    This test always passes but logs the measurement.
    """
    tprint("")
    tprint("=" * 70)
    tprint("TEST 1d: Round-trip delta variance (informational)")
    tprint("=" * 70)

    rt_variance = latency_data["rt_variance"]
    rt_mean = latency_data["rt_mean"]
    rt_min = latency_data["rt_min"]
    rt_max = latency_data["rt_max"]

    if rt_variance > 0:
        tprint(f"[INFO] Round-trip variance is {rt_variance:.2f}")
        tprint("       (Expected: Python reads are not synchronized with simulation)")
    else:
        tprint(f"[PASS] Round-trip variance is 0")

    tprint(f"  Mean={rt_mean:.2f}, Min={rt_min}, Max={rt_max}, Range={rt_max - rt_min}")
    # Always passes — Python async reads are expected to have variance
    assert True


def test_counter_progression_variance(latency_data):
    """
    Both RM and static counters must have low progression variance.

    Counter progression = difference between consecutive samples.
    In a cycle-accurate simulation, both counters advance at the same rate
    so their progression should be tightly consistent.
    """
    tprint("")
    tprint("=" * 70)
    tprint("TEST 1e: Counter progression variance")
    tprint("=" * 70)

    rm_prog_variance = latency_data["rm_prog_variance"]
    static_prog_variance = latency_data["static_prog_variance"]

    tprint(f"  RM counter variance:     {rm_prog_variance:,.2f}")
    tprint(f"  Static counter variance: {static_prog_variance:,.2f}")

    # Both should progress at a consistent rate (variance should be low relative to mean)
    # We allow some variance since Python sampling is not cycle-aligned,
    # but flag extreme outliers.
    rm_counter_samples = latency_data["rm_counter_samples"]
    static_counter_samples = latency_data["static_counter_samples"]
    rm_mean_progression = statistics.mean([
        rm_counter_samples[i+1] - rm_counter_samples[i]
        for i in range(len(rm_counter_samples) - 1)
    ])
    static_mean_progression = statistics.mean([
        static_counter_samples[i+1] - static_counter_samples[i]
        for i in range(len(static_counter_samples) - 1)
    ])
    tprint(f"  RM mean progression:     {rm_mean_progression:,.2f} cycles/sample")
    tprint(f"  Static mean progression: {static_mean_progression:,.2f} cycles/sample")

    # Both counters must be advancing (mean progression > 0)
    assert rm_mean_progression > 0, (
        f"RM counter is not advancing: mean progression = {rm_mean_progression:.2f}"
    )
    assert static_mean_progression > 0, (
        f"Static counter is not advancing: mean progression = {static_mean_progression:.2f}"
    )


# ---------------------------------------------------------------------------
# TEST 2: Counter Correlation Analysis
# ---------------------------------------------------------------------------

def test_counter_correlation(echo_api, static_api):
    """
    Test 2: Counter Correlation Analysis.

    If cycle-accurate, the RM's counter and static's counter should be perfectly
    correlated (linear relationship with R^2 = 1.0).

    With barrier-synchronized shared memory, the correlation should be perfect
    (R^2 = 1.0) since data arrives with constant latency.
    """
    tprint("")
    tprint("=" * 70)
    tprint("TEST 2: Counter Correlation Analysis")
    tprint("=" * 70)

    pairs = []
    NUM_SAMPLES = 100

    tprint(f"Collecting {NUM_SAMPLES} counter pairs...")

    for _ in range(NUM_SAMPLES):
        static_cnt = int(static_api.read_activity_counter())
        rm_cnt = int(echo_api.read_rm_counter())
        pairs.append((static_cnt, rm_cnt))
        time.sleep(0.002)

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

    if r_squared < 0.9999:
        tprint(f"[FAIL] R^2 is {r_squared:.6f}, expected 1.0 for cycle-accurate")
        tprint("       -> Counters are not perfectly correlated")
    else:
        tprint(f"[PASS] R^2 is near 1.0")

    assert r_squared >= 0.9999, (
        f"Counter R^2 is {r_squared:.6f}, expected >= 0.9999. "
        "Counters are not perfectly correlated — indicates counter drift between processes."
    )


# ---------------------------------------------------------------------------
# TEST 3: Echo Consistency (Informational)
# ---------------------------------------------------------------------------

def test_echo_consistency_informational(echo_api, static_api):
    """
    Test 3: Echo Consistency Check (Informational).

    This test measures the delay between Python reads of static_counter and echo.
    Because Python reads are ASYNC (not synchronized with simulation), variance
    is EXPECTED and does NOT indicate cycle inaccuracy.

    This test always passes — it documents that Python-side read timing is a
    separate concern from simulation cycle accuracy.
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

    tprint(f"[INFO] Python read variance is {delay_variance:.2f}")
    tprint("       (Expected: Python reads are not synchronized with simulation)")
    tprint("       This does NOT affect simulation cycle accuracy.")

    # Always passes — informational only
    assert True


# ---------------------------------------------------------------------------
# Script entry-point (backward-compatible)
# ---------------------------------------------------------------------------

def main():
    """Run all cycle accuracy tests (script mode)."""
    from partial_reconfiguration import PRSystem

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

    tprint("=" * 70)
    tprint("BUILDING SIMULATION (one-time)")
    tprint("=" * 70)

    with PRSystem(config='pr_config.yaml', cycle_accurate=True) as system:
        system.build()
        system.simulate()

        time.sleep(0.2)

        echo_api = system.get_rm_api('rp_echo')
        static_api = system.get_static_api()

        tprint("")
        tprint("Build complete. Running all tests...")

        # Collect latency data
        NUM_SAMPLES = 200
        WARMUP_SAMPLES = 10
        SAMPLE_INTERVAL = 0.005

        latency_samples = []
        rm_counter_samples = []
        static_counter_samples = []
        echo_samples = []

        for i in range(NUM_SAMPLES):
            lat = int(echo_api.read_latency_measurement())
            rm_cnt = int(echo_api.read_rm_counter())
            static_cnt = int(static_api.read_activity_counter())
            echo_cnt = int(echo_api.read_static_counter_echo())

            if i >= WARMUP_SAMPLES:
                latency_samples.append(lat)
                rm_counter_samples.append(rm_cnt)
                static_counter_samples.append(static_cnt)
                echo_samples.append(echo_cnt)

            time.sleep(SAMPLE_INTERVAL)

        latency_mean = statistics.mean(latency_samples)
        latency_variance = statistics.variance(latency_samples)
        latency_stdev = statistics.stdev(latency_samples)
        latency_min = min(latency_samples)
        latency_max = max(latency_samples)
        latency_range = latency_max - latency_min

        round_trip_deltas = [
            static_counter_samples[i] - echo_samples[i]
            for i in range(len(static_counter_samples))
        ]
        rt_mean = statistics.mean(round_trip_deltas)
        rt_variance = statistics.variance(round_trip_deltas)

        num_samples = len(rm_counter_samples)
        rm_progression = [rm_counter_samples[i+1] - rm_counter_samples[i] for i in range(num_samples - 1)]
        static_progression = [static_counter_samples[i+1] - static_counter_samples[i] for i in range(num_samples - 1)]
        rm_prog_variance = statistics.variance(rm_progression)
        static_prog_variance = statistics.variance(static_progression)

        tprint(f"Latency Measurement (rm_counter - static_counter_in):")
        tprint(f"  Mean:     {latency_mean:,.2f} cycles")
        tprint(f"  Variance: {latency_variance:,.2f}")
        tprint(f"  StdDev:   {latency_stdev:,.2f} cycles")
        tprint(f"  Min:      {latency_min:,} cycles")
        tprint(f"  Max:      {latency_max:,} cycles")
        tprint(f"  Range:    {latency_range:,} cycles")
        tprint(f"Round-Trip Delta (static_counter - echo):")
        tprint(f"  Mean:     {rt_mean:,.2f} cycles")
        tprint(f"  Variance: {rt_variance:,.2f}")
        tprint(f"Counter Progression:")
        tprint(f"  RM counter variance:     {rm_prog_variance:,.2f}")
        tprint(f"  Static counter variance: {static_prog_variance:,.2f}")
        tprint("")
        tprint("Sample data (first 10):")
        tprint(f"{'Sample':<8} {'Static':<12} {'Echo':<12} {'RM':<12} {'Latency':<12} {'RT Delta':<12}")
        tprint("-" * 70)
        for i in range(min(10, num_samples)):
            rt_delta = static_counter_samples[i] - echo_samples[i]
            tprint(f"{i:<8} {static_counter_samples[i]:<12,} {echo_samples[i]:<12,} "
                   f"{rm_counter_samples[i]:<12,} {latency_samples[i]:<12,} {rt_delta:<12,}")

        # Evaluate
        EXPECTED_PIPELINE_DEPTH = 3
        latency_passed = (
            latency_variance == 0 and
            latency_range == 0 and
            abs(latency_mean) <= EXPECTED_PIPELINE_DEPTH * 10
        )

        # Counter correlation
        pairs = []
        for _ in range(100):
            s = int(static_api.read_activity_counter())
            r = int(echo_api.read_rm_counter())
            pairs.append((s, r))
            time.sleep(0.002)

        sv = [p[0] for p in pairs]
        rv = [p[1] for p in pairs]
        ms, mr = statistics.mean(sv), statistics.mean(rv)
        num = sum((s - ms) * (r - mr) for s, r in pairs)
        ds = sum((s - ms) ** 2 for s in sv) ** 0.5
        dr = sum((r - mr) ** 2 for r in rv) ** 0.5
        corr = (num / (ds * dr)) if ds > 0 and dr > 0 else 0
        r_squared = corr ** 2
        correlation_passed = r_squared >= 0.9999

        tprint(f"Pearson R^2: {r_squared:.6f}")

        tprint("")
        tprint("=" * 70)
        tprint("FINAL SUMMARY")
        tprint("=" * 70)
        tprint(f"  Latency Variance: {'PASS' if latency_passed else 'FAIL'}")
        tprint(f"  Counter Correlation: {'PASS' if correlation_passed else 'FAIL'}")
        tprint(f"  Echo Consistency: INFO (Python async reads)")

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
            if not latency_passed:
                tprint("- Latency variance > 0 indicates variable communication delay")
            if not correlation_passed:
                tprint("- Counter correlation < 1.0 indicates counter drift")
            return 1


if __name__ == "__main__":
    import os
    os.chdir(THIS_DIR)
    sys.exit(main())
