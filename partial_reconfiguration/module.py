from typing import Dict, List, Optional, Any, TYPE_CHECKING, Union
from pathlib import Path
import subprocess
import logging

from .exceptions import PRBuildError, PRReconfigurationError

if TYPE_CHECKING:
    from .system import PRSystem
    from .partition import Partition

logger = logging.getLogger(__name__)


class ReconfigurableModule:
    """
    Represents a reconfigurable module (RM) that can be loaded into a partition.

    Each RM manages:
    - Building the simulation binary
    - Starting/stopping the simulation process
    - Port mapping to partition interface
    - Fresh state on each instantiation

    The key insight is that each time we start a new process, all registers
    are initialized to their reset values - this naturally provides the
    state reset behavior that real PR requires.
    """

    def __init__(
        self,
        name: str,
        partition_name: str,
        design: str = None,
        sources: List[str] = None,
        parameters: Dict[str, Any] = None,
        port_mapping: Dict[str, str] = None,
        port_compatibility: Dict[str, Dict] = None,
        clocks: List[str] = None,
        resets: List[Any] = None,
        tieoffs: Dict[str, Any] = None,
        interfaces: Dict[str, Dict] = None,
        system: 'PRSystem' = None,
        is_greybox: bool = False,
        auto_wrap: bool = False,
        auto_wrap_config: Dict[str, Any] = None,
        ports_override: Dict[str, Dict] = None
    ):
        """
        Initialize reconfigurable module.

        Parameters
        ----------
        name : str
            Module identifier
        partition_name : str
            Target partition name
        design : str
            Design/module name for SiliconCompiler
        sources : list
            Source file paths
        parameters : dict
            Module parameters (Verilog parameters)
        port_mapping : dict
            Mapping from partition ports to RM ports
            Format: {partition_port: rm_port}
        port_compatibility : dict
            Port compatibility overrides (width, type adjustments)
        clocks : list
            Clock signal names
        resets : list
            Reset signal configs (can be strings or dicts)
        tieoffs : dict
            Tie-off configurations for unused ports
        interfaces : dict
            Interface definitions (overrides partition interface)
        system : PRSystem
            Parent system reference
        is_greybox : bool
            True if this is an auto-generated greybox module
        auto_wrap : bool
            If True, generate a Python API for reading/writing
            partition boundary signals via shared memory interface.
        auto_wrap_config : dict
            Configuration for API generation (reserved for future use).
        ports_override : dict, optional
            Explicit port definitions to use instead of RTL parsing.
            Format: {port_name: {direction: 'input'|'output', width: N, type: 'clock'|'reset'|'data'}}
            When provided, pyslang RTL parsing is skipped and these ports are used directly.
        """
        self.name = name
        self.partition_name = partition_name
        self.system = system
        self.is_greybox = is_greybox

        self.design = design
        self.sources = sources or []
        self.parameters = parameters or {}
        self.clocks = clocks or ['clk']
        self.resets = resets or []
        self.tieoffs = tieoffs or {}
        self.interfaces = interfaces or {}

        self.port_mapping = port_mapping or {}
        self.port_compatibility = port_compatibility or {}

        self.auto_wrap = auto_wrap
        self.auto_wrap_config = auto_wrap_config or {}
        self.ports_override = ports_override

        self._dut = None
        self._process: Optional[subprocess.Popen] = None
        self._binary_path: Optional[str] = None  # Path to this RM's binary
        self._built = False
        self._intf_defs: Dict[str, Dict] = {}

        self._module_info = None
        self._api_class = None

    @property
    def partition(self) -> Optional['Partition']:
        """Get the partition this RM belongs to."""
        if self.system:
            return self.system.partitions.get(self.partition_name)
        return None

    def _get_build_dir(self) -> str:
        """Get build directory for this RM."""
        if self.system and self.system.build_dir:
            return str(Path(self.system.build_dir) / 'rm' / self.name)
        return f'build/pr/rm/{self.name}'

    def _create_dut(self):
        """
        Placeholder for backward compatibility.

        In DPI mode, builds are handled centrally by VerilatorBuilder
        via PRSystem.build(). Individual RMs don't build independently.
        """
        return None

    def build(self, fast: bool = False) -> 'ReconfigurableModule':
        """
        Build the simulation artifacts for this RM.

        In DPI mode, the actual compilation is handled centrally by
        VerilatorBuilder via PRSystem.build(). This method generates
        the Python API if auto_wrap is enabled.

        Parameters
        ----------
        fast : bool
            Skip rebuild if artifacts exist

        Returns
        -------
        ReconfigurableModule
            Self for method chaining
        """
        try:
            if self.auto_wrap and self._api_class is None:
                self._generate_api()

            self._built = True
        except Exception as e:
            raise PRBuildError(f"Failed to build RM '{self.name}': {e}") from e

        return self

    def _generate_api(self):
        """
        Generate Python API for this RM using port-index access.

        Gets boundary ports from partition config. Port order:
        to_rm first (idx 0..T-1), then from_rm (T..T+F-1).
        This matches signal_access.h indices.

        Does NOT modify self.design or self.sources.
        """
        from .codegen.api_generator import ApiGenerator, PortSpec

        port_specs = self._get_boundary_ports()
        if not port_specs:
            logger.warning(f"No boundary ports found for RM '{self.name}', skipping API generation")
            return

        module_name = self.design or self.name
        generator = ApiGenerator()
        class_name = generator._class_name_from(module_name)
        self._api_class = generator.generate_api_class(class_name, module_name, port_specs)
        logger.info(f"Generated API for RM '{self.name}': {len(port_specs)} ports")

    def _get_boundary_ports(self) -> List:
        """
        Extract PortSpec list from partition config boundary.

        Returns to_rm ports first (idx 0..T-1), then from_rm (T..T+F-1).
        """
        from .codegen.api_generator import PortSpec

        if self.system is None or self.system.config is None:
            return []

        # Find partition config with boundary
        for part_cfg in self.system.config.partitions:
            if part_cfg['name'] != self.partition_name:
                continue
            if 'boundary' not in part_cfg:
                return []

            boundary = part_cfg['boundary']
            to_rm_ports = [p for p in boundary if p['direction'] == 'to_rm']
            from_rm_ports = [p for p in boundary if p['direction'] == 'from_rm']

            port_specs = []
            idx = 0
            for port in to_rm_ports:
                port_specs.append(PortSpec(
                    name=port['name'],
                    width=port.get('width', 1),
                    direction='to_rm',
                    index=idx,
                ))
                idx += 1
            for port in from_rm_ports:
                port_specs.append(PortSpec(
                    name=port['name'],
                    width=port.get('width', 1),
                    direction='from_rm',
                    index=idx,
                ))
                idx += 1

            return port_specs

        return []

    def get_api(self, shm=None):
        """
        Get the auto-generated Python API for this RM.

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
        rm = system.get_rm('my_module')
        api = rm.get_api(shm)
        api.write_counter(42)
        result = api.read_result()
        ```
        """
        if not self.auto_wrap:
            logger.warning(
                f"get_api() called on RM '{self.name}' but auto_wrap=False"
            )
            return None

        if self._api_class is None:
            raise PRBuildError(
                f"RM '{self.name}' has not been built yet. Call build() first."
            )

        return self._api_class(shm)

    def start(self, extra_plusargs: List[str] = None) -> Optional[subprocess.Popen]:
        """
        Mark the RM as started.

        In DPI mode, the RM runs as a thread inside the single simulation
        binary managed by SimulationProcessManager. This method just marks the
        RM as active.

        Parameters
        ----------
        extra_plusargs : list, optional
            Unused in DPI mode.

        Returns
        -------
        None
            No separate process in DPI mode.
        """
        if not self._built:
            self.build()

        logger.info(f"RM '{self.name}' marked as started (DPI mode)")
        return None

    def terminate(self, timeout: float = 10.0):
        """
        Mark the RM as terminated.

        In DPI mode, the RM thread lifecycle is managed by the C++ driver.
        Reconfiguration swaps the model within the existing thread.
        This method just clears the process reference.

        Parameters
        ----------
        timeout : float
            Unused in DPI mode.
        """
        self._process = None

    @property
    def is_running(self) -> bool:
        """Check if RM is currently active in a partition.

        In multi-binary mode, checks if this RM is the active RM
        in its partition and the partition's RM process is alive.
        """
        if self.system and hasattr(self.system, '_sim_process') and self.system._sim_process:
            partition = self.partition
            if partition and partition.active_rm is self:
                return self.system._sim_process.is_running
        if self._process is not None:
            return self._process.poll() is None
        return False

    @property
    def is_built(self) -> bool:
        """Check if RM has been built."""
        return self._built

    def wait(self, timeout: float = None) -> int:
        """
        Wait for the RM process to complete.

        Parameters
        ----------
        timeout : float, optional
            Maximum time to wait

        Returns
        -------
        int
            Process return code
        """
        if self._process is None:
            return 0
        return self._process.wait(timeout=timeout)

    def __repr__(self) -> str:
        status = "running" if self.is_running else ("built" if self.is_built else "not built")
        return f"<ReconfigurableModule '{self.name}' partition='{self.partition_name}' status={status}>"
