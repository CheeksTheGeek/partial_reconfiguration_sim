from typing import Dict, List, Optional, Any, TYPE_CHECKING
from pathlib import Path
import subprocess
import logging

from .boundary import PartitionBoundary
from .exceptions import PRBuildError, PRConfigError

if TYPE_CHECKING:
    from .system import PRSystem
    from .partition import Partition
    from .wrapper_generator import WrapperOutput

logger = logging.getLogger(__name__)


class StaticRegion:
    """
    Represents the static (non-reconfigurable) region of an FPGA.

    The static region:
    - Runs continuously for the entire simulation
    - Contains boundary logic for each partition
    - Manages isolation/decoupling during reconfiguration
    - Provides clocks, resets, and infrastructure to RMs

    In simulation:
    - This is a single SbDut/Verilator process that never stops
    - It connects to partition queues for communication
    - It can receive isolation signals from Python
    - RMs connect to the other end of the partition queues

    Architecture:
    ```
    StaticRegion (persistent SbDut)
        ├── clk, rst_n
        ├── Partition rp0 boundary
        │   ├── to_rp0_* (outputs to RM)
        │   ├── from_rp0_* (inputs from RM)
        │   └── rp0_isolated (isolation control)
        ├── Partition rp1 boundary
        │   └── ...
        └── Internal logic (memory controller, interconnect, etc.)
    ```
    """

    def __init__(
        self,
        name: str = 'static_region',
        design: Any = None,
        sources: List[str] = None,
        parameters: Dict[str, Any] = None,
        interfaces: Dict[str, Dict] = None,
        clocks: List[str] = None,
        resets: List[Any] = None,
        system: 'PRSystem' = None,
        build_dir: str = None,
        auto_wrap: bool = False,
        auto_wrap_config: Dict[str, Any] = None,
        ports_override: Dict[str, Dict] = None
    ):
        """
        Initialize static region.

        Parameters
        ----------
        name : str
            Static region name
        design : Design or str
            SiliconCompiler Design object or design name
        sources : list
            RTL source files
        parameters : dict
            Module parameters
        interfaces : dict
            Interface definitions (connections to partitions)
        clocks : list
            Clock signal names
        resets : list
            Reset signal configs
        system : PRSystem
            Parent system reference
        build_dir : str
            Build directory
        auto_wrap : bool
            If True, parse RTL and auto-generate UMI wrapper.
            This allows Python to read/write static region signals
            via the generated API.
        auto_wrap_config : dict
            Configuration for wrapper generation. Supported keys:
            - enable_read_back: bool (default True)
            - enable_interrupts: bool (default True)
            - address_width: int (default 16)
            - register_width: int (default 32)
            - clock_name: str (default 'clk')
            - reset_name: str (default 'rst_n')
            - reset_active_low: bool (default True)
        ports_override : dict, optional
            Explicit port definitions to use instead of RTL parsing.
            Format: {port_name: {direction: 'input'|'output', width: N, type: 'clock'|'reset'|'data'}}
            When provided, pyslang RTL parsing is skipped and these ports are used directly.
        """
        self.name = name
        self.design = design
        self.sources = sources or []
        self.parameters = parameters or {}
        self.interfaces = interfaces or {}
        self.clocks = clocks or ['clk']
        self.resets = resets or []
        self.system = system
        self.build_dir = build_dir or 'build/pr/static'
        self.auto_wrap = auto_wrap
        self.auto_wrap_config = auto_wrap_config or {}
        self.ports_override = ports_override  # Explicit port definitions (skip RTL parsing)
        self._dut = None
        self._process: Optional[subprocess.Popen] = None
        self._built = False
        self._running = False
        self._boundaries: Dict[str, PartitionBoundary] = {}
        self._intfs: Dict[str, Any] = {}
        self._wrapper_output: Optional['WrapperOutput'] = None
        self._module_info = None
        self._api_class = None

    def add_partition_boundary(
        self,
        partition: 'Partition',
        tx_interface: str = None,
        rx_interface: str = None
    ) -> PartitionBoundary:
        """
        Add a partition boundary to the static region.

        Parameters
        ----------
        partition : Partition
            The partition to connect
        tx_interface : str
            Name of static region's TX interface to this partition
        rx_interface : str
            Name of static region's RX interface from this partition

        Returns
        -------
        PartitionBoundary
            The created boundary
        """
        boundary = PartitionBoundary(partition_name=partition.name)
        self._boundaries[partition.name] = boundary
        return boundary

    def get_boundary(self, partition_name: str) -> Optional[PartitionBoundary]:
        """Get boundary for a partition."""
        return self._boundaries.get(partition_name)

    def _create_dut(self):
        """Create the SbDut for the static region."""
        if self._dut is not None:
            return self._dut

        from switchboard import SbDut
        from siliconcompiler import Design
        tool = 'verilator'
        trace = False
        trace_type = 'vcd'
        frequency = 100e6
        max_rate = -1

        if self.system:
            tool = self.system.tool
            trace = self.system.trace
            trace_type = self.system.trace_type
            frequency = self.system.frequency
            max_rate = self.system.max_rate
        design_obj = None
        if self.sources:
            design_name = self.design if isinstance(self.design, str) else self.name
            design_obj = Design(design_name)

            with design_obj.active_fileset('rtl'):
                design_obj.set_topmodule(design_name)
                for source in self.sources:
                    source_path = Path(source)
                    if source_path.exists():
                        design_obj.add_file(str(source_path))
                    elif self.system and self.system.config and self.system.config._source_path:
                        config_dir = self.system.config._source_path.parent
                        rel_path = config_dir / source
                        if rel_path.exists():
                            design_obj.add_file(str(rel_path))
                        else:
                            design_obj.add_file(source)
                    else:
                        design_obj.add_file(source)

            with design_obj.active_fileset('verilator'):
                design_obj.set_topmodule(design_name)
                design_obj.add_depfileset(design_obj, 'rtl')

            with design_obj.active_fileset('icarus'):
                design_obj.set_topmodule(design_name)
                design_obj.add_depfileset(design_obj, 'rtl')

        elif self.design:
            design_obj = self.design

        if design_obj is None:
            raise PRConfigError(
                f"Static region '{self.name}' has no design or sources. "
                "A static region requires RTL to simulate."
            )
        interfaces = self.interfaces.copy() if self.interfaces else {}
        if self.auto_wrap and self._wrapper_output:
            umi_dw = self.auto_wrap_config.get('umi_dw', 256)
            umi_aw = self.auto_wrap_config.get('umi_aw', 64)
            umi_cw = self.auto_wrap_config.get('umi_cw', 32)
            interfaces['req'] = {
                'type': 'umi',
                'dw': umi_dw,
                'aw': umi_aw,
                'cw': umi_cw,
                'direction': 'input',
                'txrx': 'umi'
            }
            interfaces['resp'] = {
                'type': 'umi',
                'dw': umi_dw,
                'aw': umi_aw,
                'cw': umi_cw,
                'direction': 'output',
                'txrx': 'umi'
            }
            logger.info(f"Added UMI interfaces for auto-wrapped static region: dw={umi_dw}, aw={umi_aw}, cw={umi_cw}")

        self._dut = SbDut(
            design=design_obj,
            tool=tool,
            trace=trace,
            trace_type=trace_type,
            frequency=frequency,
            max_rate=max_rate,
            autowrap=True,
            parameters=self.parameters,
            interfaces=interfaces,
            clocks=self.clocks,
            resets=self.resets,
            builddir=self.build_dir
        )

        return self._dut

    def build(self, fast: bool = False) -> 'StaticRegion':
        """
        Build the static region simulation.

        If auto_wrap is enabled, this will:
        1. Parse RTL sources to extract port definitions
        2. Generate UMI wrapper RTL
        3. Generate Python API, C headers, and JSON address map
        4. Build the wrapper (which includes the original module)

        Parameters
        ----------
        fast : bool
            Skip rebuild if binary exists

        Returns
        -------
        StaticRegion
            Self for method chaining
        """
        try:
            if self.auto_wrap and not self._wrapper_output:
                self._generate_auto_wrap()

            dut = self._create_dut()
            dut.build(fast=fast)
            self._built = True
            logger.info(f"Static region '{self.name}' built successfully")
        except Exception as e:
            raise PRBuildError(f"Failed to build static region '{self.name}': {e}") from e

        return self

    def _generate_auto_wrap(self):
        """
        Parse RTL and generate UMI wrapper for the static region.

        This parses the RTL sources to extract port definitions,
        then generates:
        - UMI wrapper RTL (wrapper instantiates original module)
        - Python API module
        - C header file
        - JSON address map

        If ports_override is provided, RTL parsing is skipped and the
        explicit port definitions are used directly.
        """
        from .rtl_parser import RTLParser, ModuleInfo, PortInfo
        from .wrapper_generator import UMIWrapperGenerator, WrapperConfig

        module_name = self.design if isinstance(self.design, str) else self.name
        if self.ports_override:
            logger.info(f"Using explicit ports override for static region: {self.name}")
            self._module_info = self._create_module_info_from_override(module_name)
        else:
            if not self.sources:
                raise PRBuildError(
                    f"Static region '{self.name}' has auto_wrap=True but no sources specified "
                    f"and no ports_override provided"
                )
            resolved_sources = []
            for source in self.sources:
                source_path = Path(source)
                if source_path.exists():
                    resolved_sources.append(str(source_path.resolve()))
                elif self.system and self.system.config and self.system.config._source_path:
                    config_dir = self.system.config._source_path.parent
                    rel_path = config_dir / source
                    if rel_path.exists():
                        resolved_sources.append(str(rel_path.resolve()))
                    else:
                        raise PRBuildError(f"Source file not found: {source}")
                else:
                    raise PRBuildError(f"Source file not found: {source}")

            logger.info(f"Parsing RTL for static region auto-wrap: {self.name}")
            parser = RTLParser()
            try:
                self._module_info = parser.parse_module(resolved_sources, module_name)
            except Exception as e:
                raise PRBuildError(f"Failed to parse RTL for static region '{self.name}': {e}") from e
            warnings = parser.validate_ports(self._module_info)
            for warning in warnings:
                logger.warning(f"[{self.name}] {warning}")
        clock_name = self.auto_wrap_config.get('clock_name', 'clk')
        if self.clocks:
            clock_name = self.clocks[0]

        config = WrapperConfig(
            enable_read_back=self.auto_wrap_config.get('enable_read_back', True),
            enable_interrupts=self.auto_wrap_config.get('enable_interrupts', True),
            enable_wide_burst=self.auto_wrap_config.get('enable_wide_burst', True),
            enable_inout=self.auto_wrap_config.get('enable_inout', True),
            address_width=self.auto_wrap_config.get('address_width', 16),
            register_width=self.auto_wrap_config.get('register_width', 32),
            clock_name=clock_name,
        )
        logger.info(f"Generating UMI wrapper for static region: {self.name}")
        generator = UMIWrapperGenerator(config)
        self._wrapper_output = generator.generate(self._module_info)

        if not self._wrapper_output.is_valid:
            for error in self._wrapper_output.validation_errors:
                logger.error(f"[{self.name}] {error}")
            raise PRBuildError(
                f"Auto-wrap validation failed for static region '{self.name}': "
                f"{len(self._wrapper_output.validation_errors)} errors"
            )

        for warning in self._wrapper_output.validation_warnings:
            logger.warning(f"[{self.name}] {warning}")
        build_dir = Path(self.build_dir)
        build_dir.mkdir(parents=True, exist_ok=True)

        wrapper_path = build_dir / f"{self._wrapper_output.wrapper_name}.sv"
        wrapper_path.write_text(self._wrapper_output.wrapper_rtl)
        logger.info(f"Wrote wrapper RTL: {wrapper_path}")

        api_path = build_dir / f"{self._module_info.name}_api.py"
        api_path.write_text(self._wrapper_output.python_module_code)
        logger.info(f"Wrote Python API: {api_path}")

        header_path = build_dir / f"{self._module_info.name}_regs.h"
        header_path.write_text(self._wrapper_output.c_header_code)
        logger.info(f"Wrote C header: {header_path}")

        json_path = build_dir / f"{self._module_info.name}_regs.json"
        json_path.write_text(self._wrapper_output.json_address_map)
        logger.info(f"Wrote JSON address map: {json_path}")
        self.sources = [str(wrapper_path)] + resolved_sources
        self.design = self._wrapper_output.wrapper_name

        logger.info(f"Auto-wrap complete for static region '{self.name}': {len(self._module_info.ports)} ports mapped")

    def _create_module_info_from_override(self, module_name: str):
        """
        Create ModuleInfo from explicit ports_override configuration.

        This allows users to specify port definitions in YAML config instead of
        relying on pyslang RTL parsing. Useful when:
        - pyslang cannot parse the RTL (unsupported constructs)
        - User wants to exclude certain ports from wrapping
        - User wants to override inferred port types

        Parameters
        ----------
        module_name : str
            Name of the module

        Returns
        -------
        ModuleInfo
            Module info with ports from override
        """
        from .rtl_parser import ModuleInfo, PortInfo

        ports = []
        for port_name, port_def in self.ports_override.items():
            direction = port_def.get('direction', 'input')
            width = port_def.get('width', 1)
            port_type = port_def.get('type', 'data')

            port_info = PortInfo(
                name=port_name,
                direction=direction,
                width=width,
                port_type=port_type,
                is_signed=port_def.get('signed', False),
                is_array=False,
                array_dimensions=[]
            )
            ports.append(port_info)

        return ModuleInfo(name=module_name, ports=ports)

    def get_api(self, umi=None):
        """
        Get the auto-generated Python API for this static region.

        This method returns an API class that provides typed read/write
        methods for all accessible registers, using the original port names.

        Parameters
        ----------
        umi : UmiTxRx, optional
            UMI interface to use. If not provided, will try to get from
            the static region's interfaces.

        Returns
        -------
        object
            Instance of the generated API class, or None if auto_wrap=False

        Example
        -------
        ```python
        static = system.static_region
        api = static.get_api()
        counter = api.read_count()
        led_status = api.read_led_zero_on()
        ```
        """
        if not self.auto_wrap:
            logger.warning(
                f"get_api() called on static region '{self.name}' but auto_wrap=False"
            )
            return None

        if self._wrapper_output is None:
            raise PRBuildError(
                f"Static region '{self.name}' has not been built yet. Call build() first."
            )
        if umi is None:
            if self._intfs:
                for intf_name, intf in self._intfs.items():
                    if hasattr(intf, 'read') and hasattr(intf, 'write'):
                        umi = intf
                        break

            if umi is None:
                raise ValueError(
                    f"No UMI interface provided and could not find one for static region '{self.name}'"
                )
        if self._api_class is None:
            import importlib.util
            import sys

            build_dir = Path(self.build_dir)
            api_path = build_dir / f"{self._module_info.name}_api.py"

            if not api_path.exists():
                raise PRBuildError(f"API file not found: {api_path}")
            module_name = f"generated_api_static_{self.name}"
            spec = importlib.util.spec_from_file_location(module_name, api_path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            self._api_class = getattr(module, self._wrapper_output.python_class_name)
        return self._api_class(umi)

    @property
    def wrapper_output(self) -> Optional['WrapperOutput']:
        """Get the wrapper output if auto_wrap was used."""
        return self._wrapper_output

    @property
    def address_map(self):
        """Get the address map if auto_wrap was used."""
        if self._wrapper_output:
            return self._wrapper_output.address_map
        return None

    def start(self, start_delay: float = None) -> subprocess.Popen:
        """
        Start the static region simulation.

        This starts a long-running Verilator process that will persist
        for the entire PR simulation session. Unlike RMs, the static
        region is NEVER stopped during normal operation.

        Parameters
        ----------
        start_delay : float, optional
            Delay before starting

        Returns
        -------
        subprocess.Popen
            Running process handle
        """
        if not self._built:
            self.build()

        dut = self._dut

        try:
            self._process = dut.simulate(
                start_delay=start_delay,
                intf_objs=True  # Static region owns interface objects
            )
            self._running = True
            self._intfs = dut.intfs

            logger.info(f"Static region '{self.name}' started (PID: {self._process.pid})")

        except Exception as e:
            raise PRBuildError(
                f"Failed to start static region '{self.name}': {e}"
            ) from e

        return self._process

    def terminate(self, timeout: float = 10.0):
        """
        Terminate the static region.

        This should only be called at the END of simulation.
        During normal PR operation, the static region keeps running.

        Parameters
        ----------
        timeout : float
            Timeout for graceful termination
        """
        if self._dut is not None:
            self._dut.terminate(stop_timeout=timeout)

        if self._process is not None:
            try:
                self._process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
            self._process = None

        self._running = False
        logger.info(f"Static region '{self.name}' terminated")

    def isolate_partition(self, partition_name: str):
        """
        Isolate a partition boundary.

        Called during reconfiguration to gate the partition boundary.

        Parameters
        ----------
        partition_name : str
            Name of partition to isolate
        """
        boundary = self._boundaries.get(partition_name)
        if boundary:
            boundary.isolate()
            logger.debug(f"Isolated partition '{partition_name}' in static region")

    def release_partition(self, partition_name: str):
        """
        Release partition isolation.

        Called after new RM is loaded and reset.

        Parameters
        ----------
        partition_name : str
            Name of partition to release
        """
        boundary = self._boundaries.get(partition_name)
        if boundary:
            boundary.release()
            logger.debug(f"Released partition '{partition_name}' in static region")

    @property
    def is_running(self) -> bool:
        """Check if static region is running."""
        if self._process is None:
            return False
        return self._process.poll() is None

    @property
    def is_built(self) -> bool:
        """Check if static region has been built."""
        return self._built

    @property
    def intfs(self) -> Dict[str, Any]:
        """Get interface objects for Python interaction."""
        return self._intfs

    def wait(self, timeout: float = None):
        """Wait for static region process to complete."""
        if self._process is not None:
            self._process.wait(timeout=timeout)

    def __repr__(self) -> str:
        status = "running" if self._running else ("built" if self._built else "not built")
        return f"<StaticRegion '{self.name}' status={status} partitions={list(self._boundaries.keys())}>"
