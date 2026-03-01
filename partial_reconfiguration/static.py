from typing import Dict, List, Optional, Any, TYPE_CHECKING
from pathlib import Path
import subprocess
import logging

from .boundary import PartitionBoundary
from .exceptions import PRBuildError, PRConfigError

if TYPE_CHECKING:
    from .system import PRSystem
    from .partition import Partition

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
    - This is the static Verilator process that runs continuously
    - It communicates with RM processes via shared memory + DPI-C bridges
    - It can receive isolation signals from Python via the mailbox
    - Each RM runs as a separate Verilator process sharing the same memory region

    Architecture:
    ```
    StaticRegion (persistent Verilator process)
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
            If True, generate a Python API for reading/writing
            static region signals via shared memory interface.
        auto_wrap_config : dict
            Configuration for API generation (reserved for future use).
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
        self._module_info = None
        self._api_class = None
        self._static_ports_for_api = None  # Set by system._setup_builder()

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
        """
        Placeholder for backward compatibility.

        In DPI mode, builds are handled centrally by VerilatorBuilder
        via PRSystem.build(). The static region doesn't build independently.
        """
        return None

    def build(self, fast: bool = False) -> 'StaticRegion':
        """
        Build the static region artifacts.

        In DPI mode, the actual compilation is handled centrally by
        VerilatorBuilder via PRSystem.build(). This method generates
        the Python API if auto_wrap is enabled.

        Parameters
        ----------
        fast : bool
            Skip rebuild if artifacts exist

        Returns
        -------
        StaticRegion
            Self for method chaining
        """
        try:
            if self.auto_wrap and self._api_class is None:
                self._generate_api()

            self._built = True
            logger.info(f"Static region '{self.name}' artifacts ready")
        except Exception as e:
            raise PRBuildError(f"Failed to build static region '{self.name}': {e}") from e

        return self

    def _generate_api(self):
        """
        Generate Python API for the static region using port-index access.

        Uses pyslang to parse RTL directly (or ports_override if provided)
        to get port definitions, then generates a Python API class with
        read_port/write_port methods.

        Does NOT modify self.design or self.sources.
        """
        from .codegen.api_generator import ApiGenerator, PortSpec

        module_name = self.design if isinstance(self.design, str) else self.name

        # Get ports: prefer _static_ports_for_api (set by system._setup_builder),
        # then ports_override, then pyslang parsing
        if self._static_ports_for_api is not None:
            port_specs = self._static_ports_for_api
        elif self.ports_override:
            logger.info(f"Using explicit ports override for static region: {self.name}")
            port_specs = []
            idx = 0
            for port_name, port_def in self.ports_override.items():
                ptype = port_def.get('type', 'data')
                if ptype in ('clock', 'reset'):
                    continue
                port_specs.append(PortSpec(
                    name=port_name,
                    width=port_def.get('width', 1),
                    direction=port_def.get('direction', 'input'),
                    index=idx,
                ))
                idx += 1
        else:
            port_specs = self._parse_ports_with_pyslang(module_name)

        generator = ApiGenerator()
        class_name = generator._class_name_from(module_name)
        self._api_class = generator.generate_api_class(class_name, module_name, port_specs)
        logger.info(f"Generated API for static region '{self.name}': {len(port_specs)} ports")

    def _parse_ports_with_pyslang(self, module_name: str):
        """Parse RTL with pyslang and return list of PortSpec."""
        from .codegen.api_generator import PortSpec

        if not self.sources:
            raise PRBuildError(
                f"Static region '{self.name}' has auto_wrap=True but no sources specified "
                f"and no ports_override provided"
            )

        resolved_sources = self._resolve_sources()

        import pyslang

        # Read all source files
        rtl_texts = []
        for src in resolved_sources:
            rtl_texts.append(Path(src).read_text())
        combined = '\n'.join(rtl_texts)

        tree = pyslang.SyntaxTree.fromText(combined)
        comp = pyslang.Compilation()
        comp.addSyntaxTree(tree)
        comp.getAllDiagnostics()  # triggers elaboration

        clk_rst_names = {'clk', 'rst', 'reset', 'rst_n', 'reset_n'}
        port_specs = []
        idx = 0

        for inst in comp.getRoot().topInstances:
            if inst.name != module_name:
                continue
            for member in inst.body:
                if member.kind != pyslang.SymbolKind.Port:
                    continue
                if member.name in clk_rst_names:
                    continue
                direction = 'input' if 'In' in str(member.direction) else 'output'
                width = member.internalSymbol.type.bitWidth
                port_specs.append(PortSpec(
                    name=member.name,
                    width=width,
                    direction=direction,
                    index=idx,
                ))
                idx += 1
            break

        return port_specs

    def _resolve_sources(self) -> List[str]:
        """Resolve source file paths."""
        resolved = []
        for source in self.sources:
            source_path = Path(source)
            if source_path.exists():
                resolved.append(str(source_path.resolve()))
            elif self.system and self.system.config and self.system.config._source_path:
                config_dir = self.system.config._source_path.parent
                rel_path = config_dir / source
                if rel_path.exists():
                    resolved.append(str(rel_path.resolve()))
                else:
                    raise PRBuildError(f"Source file not found: {source}")
            else:
                raise PRBuildError(f"Source file not found: {source}")
        return resolved

    def _create_module_info_from_override(self, module_name: str):
        """
        Create ModuleInfo from explicit ports_override configuration.

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

    def get_api(self, shm=None):
        """
        Get the auto-generated Python API for this static region.

        Parameters
        ----------
        shm : SharedMemoryInterface, optional
            Shared memory interface to use.

        Returns
        -------
        object
            Instance of the generated API class, or None if auto_wrap=False

        Example
        -------
        ```python
        static = system.static_region
        api = static.get_api(shm)
        counter = api.read_count()
        ```
        """
        if not self.auto_wrap:
            logger.warning(
                f"get_api() called on static region '{self.name}' but auto_wrap=False"
            )
            return None

        if self._api_class is None:
            raise PRBuildError(
                f"Static region '{self.name}' has not been built yet. Call build() first."
            )

        return self._api_class(shm)

    def start(
        self,
        start_delay: float = None,
        extra_plusargs: List[str] = None
    ) -> Optional[subprocess.Popen]:
        """
        Mark the static region as started.

        In DPI mode, the static region runs as a thread inside the single
        simulation binary managed by SimulationProcessManager. This method just
        marks the static region as running.

        Parameters
        ----------
        start_delay : float, optional
            Unused in DPI mode.
        extra_plusargs : list, optional
            Unused in DPI mode.

        Returns
        -------
        None
            No separate process in DPI mode.
        """
        if not self._built:
            self.build()

        self._running = True
        logger.info(f"Static region '{self.name}' marked as started (DPI mode)")
        return None

    def terminate(self, timeout: float = 10.0):
        """
        Mark the static region as terminated.

        In DPI mode, the static region thread lifecycle is managed
        by the C++ driver.

        Parameters
        ----------
        timeout : float
            Unused in DPI mode.
        """
        self._process = None
        self._running = False
        logger.info(f"Static region '{self.name}' marked as terminated (DPI mode)")

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
        """Check if static region is running.

        In multi-binary mode, checks the system's process manager.
        Falls back to the _running flag if no process manager.
        """
        if self.system and hasattr(self.system, '_sim_process') and self.system._sim_process:
            return self.system._sim_process.is_running
        return self._running

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
