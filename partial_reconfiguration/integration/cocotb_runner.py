"""
Cocotb Runner for PR simulation.

Orchestrates:
1. Building RM binaries (rm_only mode)
2. Creating SHM files and starting RM processes
3. Building and running the cocotb simulation (static region + DPI bridges)
4. Cleanup on exit

The static region runs inside cocotb's Verilator process. RM binaries run
as separate processes communicating via mmap'd shared memory and barriers.

Cross-process reconfiguration protocol
---------------------------------------
cocotb's Python runs embedded inside a new Verilator subprocess, so PRSystem
objects can't be shared directly.  Instead a tiny SHM region (ctrl_mailbox.shm)
in the existing PR_SHM_DIR is used:

  PR_CTRL_SHM env var  →  absolute path to ctrl_mailbox.shm

  Layout: see CtrlMailbox in cocotb_mode.py
    status=PENDING  → control thread calls pr_system.reconfigure()
    status=OK/ERROR → test reads result, resets to IDLE

A background thread in the parent polls the SHM mailbox at ~5 ms intervals.
The cocotb test polls using cocotb Timer ticks (or time.sleep in synctest mode).
"""
from typing import Dict, Optional
from pathlib import Path
import logging
import threading
import time

logger = logging.getLogger(__name__)


def _control_thread(pr_system, ctrl_shm_path: str, stop_event: threading.Event):
    """
    Background thread: polls SHM ctrl_mailbox for reconfiguration requests.
    """
    from .cocotb_mode import CtrlMailbox
    mb = CtrlMailbox(ctrl_shm_path, create=False)
    last_seq = -1
    print(f"[control] thread started, SHM={ctrl_shm_path}", flush=True)

    while not stop_event.is_set():
        if mb.status == CtrlMailbox.STATUS_PENDING:
            seq = mb.seq_req
            if seq != last_seq:
                last_seq = seq
                partition = mb.partition
                rm = mb.rm_name
                print(f"[control] reconfigure {partition} -> {rm}", flush=True)
                try:
                    pr_system.reconfigure(partition, rm)
                    mb.complete_ok(seq)
                    print(f"[control] reconfigure complete", flush=True)
                except Exception as e:
                    mb.complete_error(seq, str(e))
                    print(f"[control] reconfigure failed: {e}", flush=True)
        time.sleep(0.005)

    mb.close()


class PRCocotbRunner:
    """Builds and runs a cocotb-based PR simulation.

    Parameters
    ----------
    pr_system : PRSystem
        The configured PRSystem instance (after add_partition/add_rm calls).
    test_module : str
        Python module name containing cocotb tests (e.g. 'my_test').
    test_dir : str or Path, optional
        Directory containing the test module. Defaults to cwd.
    extra_env : dict, optional
        Additional environment variables for the cocotb test process.
    hwh_location_dir : str or Path, optional
        Directory containing the HWH file (for cocotbpynq overlay).
    waves : bool
        Enable waveform dumping.
    """

    def __init__(
        self,
        pr_system,
        test_module: str,
        test_dir: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
        hwh_location_dir: Optional[str] = None,
        waves: bool = False,
    ):
        self.pr_system = pr_system
        self.test_module = test_module
        self.test_dir = Path(test_dir) if test_dir else Path.cwd()
        self.extra_env = extra_env or {}
        self.hwh_location_dir = hwh_location_dir
        self.waves = waves

    def _generate_hwh(self, build_dir: Path) -> Path:
        """
        Auto-generate a HWH file from the PRConfig's static_region.interfaces.

        Returns the directory containing the generated .hwh file so it can
        be passed to cocotbpynq via HWH_LOCATION_DIR.
        """
        from ..codegen.hwh_generator import HwhGenerator

        builder = self.pr_system._builder
        design_name      = builder._static_design or 'static_region'
        clock_name       = builder._static_clock_name or 'clk'
        reset_name       = builder._static_reset_name       # may be None
        reset_active_low = builder._static_reset_active_low

        sr = (self.pr_system.config.static_region or {}) if self.pr_system.config else {}
        interfaces = sr.get('interfaces', {})

        hwh_dir = build_dir / 'hwh'
        gen = HwhGenerator(build_dir=str(hwh_dir))
        gen.generate(
            design_name=design_name,
            interfaces=interfaces,
            clock_name=clock_name,
            reset_name=reset_name,
            reset_active_low=reset_active_low,
            output_name='design',
        )
        logger.info(f"Auto-generated HWH in {hwh_dir}")
        return hwh_dir

    def run(self):
        """Execute the full cocotb PR simulation.

        Steps:
        1. Build with cocotb_mode=True (generates bridges, barrier C++, RM binaries)
        2. Create SHM files and start RM processes
        3. Create SHM ctrl_mailbox and start control thread
        4. Build cocotb simulation (static region + DPI bridges)
        5. Run cocotb test
        6. Cleanup
        """
        from cocotb_tools.runner import get_runner
        from .cocotb_mode import CtrlMailbox

        builder   = self.pr_system._builder
        build_dir = builder.build_dir
        dpi_dir   = build_dir / 'dpi'
        bridges_dir = build_dir / 'bridges'

        # Step 1: Build
        logger.info("Step 1: Building PR simulation (cocotb mode)...")
        self.pr_system.build(cocotb_mode=True)

        # Step 2: Create SHM and start RM processes
        logger.info("Step 2: Creating SHM and starting RM processes...")
        process_mgr = self.pr_system._start_rm_processes_only()

        # Step 3: Create SHM ctrl mailbox and start control thread
        ctrl_shm_path = str(process_mgr.shm_dir.resolve() / 'ctrl_mailbox.shm')
        CtrlMailbox(ctrl_shm_path, create=True).close()   # create and initialise

        stop_event = threading.Event()
        ctrl_thread = threading.Thread(
            target=_control_thread,
            args=(self.pr_system, ctrl_shm_path, stop_event),
            daemon=True,
        )
        ctrl_thread.start()

        try:
            # Step 4: Build cocotb simulation
            logger.info("Step 4: Building cocotb simulation...")
            runner = get_runner("verilator")

            sv_sources = list(builder._static_sources)
            cocotb_top = bridges_dir / 'pr_cocotb_top.sv'
            if cocotb_top.exists():
                sv_sources.append(str(cocotb_top))

            cpp_files = []
            barrier_cpp = dpi_dir / 'pr_cocotb_barrier.cpp'
            if barrier_cpp.exists():
                cpp_files.append(str(barrier_cpp))
            for cpp_path in sorted(dpi_dir.glob('dpi_static_*.cpp')):
                cpp_files.append(str(cpp_path))

            build_args = [
                '-CFLAGS', f'-I{dpi_dir}',
                '--public-flat-rw',
                '-Wno-WIDTHTRUNC',
            ]
            for cpp_file in cpp_files:
                build_args.append(cpp_file)
            for inc_dir in builder._static_include_dirs:
                build_args.extend(['-I', inc_dir])

            runner.build(
                sources=[str(s) for s in sv_sources],
                hdl_toplevel='pr_cocotb_top',
                always=True,
                build_args=build_args,
                timescale=('1ns', '1ps'),
                waves=self.waves,
            )

            # Step 5: Run cocotb test
            logger.info("Step 5: Running cocotb test...")

            hwh_dir = self.hwh_location_dir
            if hwh_dir is None:
                hwh_dir = self._generate_hwh(build_dir)

            env = {
                'PR_SHM_DIR':        str(process_mgr.shm_dir.resolve()),
                'PR_CTRL_SHM':       ctrl_shm_path,
                'HWH_LOCATION_DIR':  str(hwh_dir),
                'COCOTB_IS_RUNNING': '1',
            }
            env.update(self.extra_env)

            runner.test(
                hdl_toplevel='pr_cocotb_top',
                hdl_toplevel_lang='verilog',
                test_dir=str(self.test_dir),
                test_module=[self.test_module],
                extra_env=env,
                waves=self.waves,
            )

        finally:
            # Step 6: Cleanup
            stop_event.set()
            ctrl_thread.join(timeout=2.0)
            process_mgr.terminate()
