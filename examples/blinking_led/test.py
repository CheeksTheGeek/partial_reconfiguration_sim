#!/usr/bin/env python3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from partial_reconfiguration import PRSystem


THIS_DIR = Path(__file__).resolve().parent
CONFIG_PATH = THIS_DIR / 'pr_config.yaml'


def test_persona(system, persona_name: str, expected_tap: int):
    """Test a persona by writing counter values and reading LED outputs."""
    print(f"\n  Testing {persona_name} (COUNTER_TAP={expected_tap})...")

    api = system.get_rm_api('pr_partition')

    test_values = [
        (0, 0, 0),
        (1 << expected_tap, 1, 1),
        ((1 << expected_tap) - 1, 0, 0),
        (0xFFFFFFFF, 1, 1),
    ]

    all_passed = True
    for counter_val, expected_led2, expected_led3 in test_values:
        api.write_counter(counter_val)

        for _ in range(3):
            readback = api.read_counter()

        print(f"      DEBUG: wrote 0x{counter_val:08X}, readback 0x{readback:08X}")

        led2 = api.read_led_two_on()
        led3 = api.read_led_three_on()

        if led2 == expected_led2 and led3 == expected_led3:
            status = "PASS"
        else:
            status = "FAIL"
            all_passed = False

        print(f"    counter=0x{counter_val:08X}: LED2={led2}, LED3={led3} "
              f"(expected {expected_led2}, {expected_led3}) [{status}]")

    return all_passed


def test_empty_persona(system):
    """Test the empty persona (LEDs always off)."""
    print(f"\n  Testing blinking_led_empty (LEDs always off)...")
    api = system.get_rm_api('pr_partition')

    all_passed = True
    for counter_val in [0, 0x00800000, 0x08000000, 0xFFFFFFFF]:
        api.write_counter(counter_val)
        time.sleep(0.01)

        led2 = api.read_led_two_on()
        led3 = api.read_led_three_on()

        if led2 == 0 and led3 == 0:
            status = "PASS"
        else:
            status = "FAIL"
            all_passed = False

        print(f"    counter=0x{counter_val:08X}: LED2={led2}, LED3={led3} "
              f"(expected 0, 0) [{status}]")

    return all_passed


def main():
    print("=" * 70)
    print("Intel Agilex 7 Blinking LED PR Example")
    print("Using UNMODIFIED Intel RTL with auto-wrap")
    print("=" * 70)

    with PRSystem(config=str(CONFIG_PATH)) as system:

        print("\n[Step 1] Building static region and RMs...")
        system.build()

        print(f"    Static region: {system.static_region.name}")
        print(f"    Partitions: {list(system.partitions.keys())}")
        print(f"    RMs: {list(system.modules.keys())}")

        print("\n[Step 2] Starting simulation...")
        system.simulate()

        print("\n[Step 3] Loading blinking_led...")
        system.load('pr_partition', 'blinking_led')

        import time
        time.sleep(0.5)
        print("    Waited for RM initialization")

        all_passed = True
        if not test_persona(system, 'blinking_led', 23):
            all_passed = False

        print("\n[Step 4] Reconfiguring to blinking_led_slow...")
        system.reconfigure('pr_partition', 'blinking_led_slow')

        import time
        time.sleep(0.5)
        print("    Waited for RM initialization")

        if not test_persona(system, 'blinking_led_slow', 27):
            all_passed = False

        print("\n[Step 5] Reconfiguring to blinking_led_empty...")
        system.reconfigure('pr_partition', 'blinking_led_empty')
        time.sleep(0.5)

        if not test_empty_persona(system):
            all_passed = False
        print("\n[Step 6] Reconfiguring back to blinking_led...")
        system.reconfigure('pr_partition', 'blinking_led')
        time.sleep(0.5)

        if not test_persona(system, 'blinking_led', 23):
            all_passed = False
        if all_passed:
            print("What to notice in the logs above!")
            print("  - Intel RTL used with ZERO modifications")
            print("  - Static region + PR partition architecture")
            print("  - Auto-wrap: pyslang parses RTL, generates Python API")
            print("  - Python API with original signal names")
            print("  - PR swap: load() and reconfigure() swap personas")
        else:
            print("SOME TESTS FAILED!")

        return 0 if all_passed else 1


if __name__ == '__main__':
    sys.exit(main())
