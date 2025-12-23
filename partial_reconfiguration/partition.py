from typing import Dict, List, Optional, Any, TYPE_CHECKING
import logging

from .exceptions import PRReconfigurationError, PRValidationError
from .boundary import PartitionBoundary
from .reconfiguration import ReconfigurationController, ReconfigurationPhase, ResetBehavior

if TYPE_CHECKING:
    from .system import PRSystem
    from .module import ReconfigurableModule

logger = logging.getLogger(__name__)


class Partition:
    """
    Logical container for a reconfigurable partition.

    A partition:
    - Defines the interface (ports) between static region and RMs
    - Owns queue URIs that remain constant across RM swaps
    - Manages which RM is currently loaded
    - Supports greybox modules for testing

    Key insight: When we swap RMs, the queues persist but the process
    changes. The new process connects to the same queues, enabling
    seamless communication transition.

    Thread Safety
    -------------
    This class is NOT thread-safe. All operations (load_rm, reconfigure,
    umi access) should be called from a single thread. If you need
    concurrent access, implement external synchronization.
    """

    def __init__(
        self,
        name: str,
        interface: Dict[str, Dict[str, Any]],
        system: 'PRSystem' = None,
        greybox: bool = False,
        initial_rm: str = None,
        reset_behavior: ResetBehavior = ResetBehavior.FRESH
    ):
        """
        Initialize partition.

        Parameters
        ----------
        name : str
            Partition identifier
        interface : dict
            Port interface definition
            Format: {port_name: {type, direction, dw, ...}}
        system : PRSystem
            Parent system reference
        greybox : bool
            Whether to auto-generate a greybox module for this partition
        initial_rm : str
            Name of RM to load initially (optional)
        reset_behavior : ResetBehavior
            How to handle reset after reconfiguration (default: FRESH)
        """
        self.name = name
        self.interface = interface
        self.system = system
        self.enable_greybox = greybox
        self.initial_rm_name = initial_rm
        self.reset_behavior = reset_behavior

        self.registered_rms: Dict[str, 'ReconfigurableModule'] = {}
        self.active_rm: Optional['ReconfigurableModule'] = None
        self._greybox_rm: Optional['ReconfigurableModule'] = None
        self._queue_uris: Dict[str, str] = {}
        self._generate_queue_uris()
        self._intfs: Dict[str, Any] = {}
        self._umi = None
        self._boundary = PartitionBoundary(partition_name=name)
        self._reconfig_controller = ReconfigurationController(
            partition=self,
            reset_behavior=reset_behavior
        )

    def _generate_queue_uris(self):
        """Generate unique queue URIs for partition ports.

        Uses absolute paths in a shared queue directory so that Python
        and Verilator processes can find the same queue files regardless
        of their working directory.
        """
        from pathlib import Path

        if self.system and self.system.build_dir:
            queue_dir = Path(self.system.build_dir).resolve() / 'queues'
            queue_dir.mkdir(parents=True, exist_ok=True)
        else:
            queue_dir = Path.cwd() / 'build' / 'queues'
            queue_dir.mkdir(parents=True, exist_ok=True)

        for port_name, port_def in self.interface.items():
            port_type = port_def.get('type', 'sb')
            direction = port_def.get('direction', 'input')

            if port_type in ['umi', 'sb']:
                self._queue_uris[port_name] = str(queue_dir / f"{self.name}_{port_name}.q")
            elif port_type in ['axi', 'axil', 'apb']:
                self._queue_uris[port_name] = str(queue_dir / f"{self.name}_{port_name}")
            elif port_type == 'gpio':
                self._queue_uris[port_name] = str(queue_dir / f"{self.name}_{port_name}_gpio.q")
            else:
                self._queue_uris[port_name] = str(queue_dir / f"{self.name}_{port_name}.q")

    def register_rm(self, rm: 'ReconfigurableModule'):
        """
        Register an RM as compatible with this partition.

        Parameters
        ----------
        rm : ReconfigurableModule
            RM to register

        Raises
        ------
        PRValidationError
            If RM partition doesn't match
        """
        if rm.partition_name != self.name:
            raise PRValidationError(
                f"Cannot register RM '{rm.name}' with partition '{self.name}': "
                f"RM belongs to partition '{rm.partition_name}'"
            )

        self.registered_rms[rm.name] = rm

    def get_queue_uri(self, port_name: str) -> Optional[str]:
        """Get queue URI for a partition port."""
        return self._queue_uris.get(port_name)

    def get_all_queue_uris(self) -> Dict[str, str]:
        """Get all queue URIs for this partition."""
        return dict(self._queue_uris)

    def create_greybox(self) -> 'ReconfigurableModule':
        """
        Create a greybox RM for this partition.

        Returns
        -------
        ReconfigurableModule
            Greybox module
        """
        if self._greybox_rm is not None:
            return self._greybox_rm

        from .greybox import GreyboxGenerator
        from .module import ReconfigurableModule

        build_dir = 'build/pr/greybox'
        if self.system and self.system.build_dir:
            build_dir = f"{self.system.build_dir}/greybox"

        clocks = []
        resets = []
        for rm in self.registered_rms.values():
            if not rm.is_greybox:
                clocks = rm.clocks or []
                resets = rm.resets or []
                break

        generator = GreyboxGenerator(build_dir=build_dir)
        greybox_path = generator.generate(
            partition_name=self.name,
            interface=self.interface,
            clocks=clocks,
            resets=resets
        )

        greybox_name = f"{self.name}_greybox"

        port_mapping = {}
        for port_name in self.interface:
            port_mapping[port_name] = port_name

        self._greybox_rm = ReconfigurableModule(
            name=greybox_name,
            partition_name=self.name,
            design=greybox_name,
            sources=[str(greybox_path)],
            port_mapping=port_mapping,
            clocks=clocks,
            resets=resets,
            system=self.system,
            is_greybox=True
        )

        self.registered_rms[greybox_name] = self._greybox_rm

        return self._greybox_rm

    def load_rm(
        self,
        rm: 'ReconfigurableModule',
        extra_plusargs: List[str] = None
    ) -> bool:
        """
        Load an RM into this partition.

        If an RM is already loaded, it will be unloaded first.

        Parameters
        ----------
        rm : ReconfigurableModule
            RM to load
        extra_plusargs : list, optional
            Additional plusargs to pass to the simulator.
            Used for barrier synchronization in cycle-accurate mode.

        Returns
        -------
        bool
            Success status

        Raises
        ------
        PRReconfigurationError
            If RM is incompatible or load fails
        """
        if rm.partition_name != self.name:
            raise PRReconfigurationError(
                f"RM '{rm.name}' belongs to partition '{rm.partition_name}', "
                f"cannot load into partition '{self.name}'"
            )
        if self.active_rm is not None:
            self.unload_rm()

        rm.configure_queues(self._queue_uris)

        rm.start(extra_plusargs=extra_plusargs)

        self.active_rm = rm
        return True

    def unload_rm(self) -> bool:
        """
        Unload current RM from partition.

        Returns
        -------
        bool
            Success status
        """
        if self.active_rm is None:
            return True
        self.active_rm.terminate()
        self.active_rm = None
        return True

    def reconfigure(
        self,
        new_rm: 'ReconfigurableModule',
        timeout: float = 10.0,
        config_time_ms: float = None
    ) -> bool:
        """
        Reconfigure partition with a new RM using proper PR phases.

        This is the main reconfiguration API. It follows real FPGA PR phases:
        1. QUIESCE - Drain in-flight transactions
        2. ISOLATE - Gate the partition boundary
        3. SWAP - Terminate old RM, start new RM
        4. RESET - Apply reset to new RM
        5. ENABLE - Release isolation

        The new RM starts with reset register values because it's
        a fresh process - this matches real PR behavior (GSR on Xilinx).

        Parameters
        ----------
        new_rm : ReconfigurableModule
            New RM to load
        timeout : float
            Timeout for graceful termination
        config_time_ms : float, optional
            Configuration time to simulate (uses system timing model if None)

        Returns
        -------
        bool
            Success status

        Raises
        ------
        PRReconfigurationError
            If reconfiguration fails
        """
        old_name = self.active_rm.name if self.active_rm else None

        if config_time_ms is None and self.system:
            timing_model = getattr(self.system, '_timing_model', None)
            if timing_model and timing_model.enabled:
                rm_config_time = getattr(new_rm, 'config_time_ms', None)
                config_time_ms = timing_model.get_config_time_ms(
                    new_rm.name, config_time_ms=rm_config_time
                )

        if config_time_ms is None:
            config_time_ms = 0.0

        def do_swap():
            """The actual swap operation."""
            if self.active_rm is not None:
                self.active_rm.terminate(timeout=timeout)

            new_rm.configure_queues(self._queue_uris)

            new_rm.start()

            self.active_rm = new_rm

        try:
            success = self._reconfig_controller.execute_full_sequence(
                swap_callback=do_swap,
                config_time_ms=config_time_ms
            )

            if success:
                logger.info(
                    f"Partition '{self.name}': reconfigured "
                    f"'{old_name}' -> '{new_rm.name}'"
                    + (f" ({config_time_ms:.1f}ms)" if config_time_ms > 0 else "")
                )

            return success

        except Exception as e:
            raise PRReconfigurationError(
                f"Failed to reconfigure partition '{self.name}' "
                f"from '{old_name}' to '{new_rm.name}': {e}"
            ) from e

    def reconfigure_simple(
        self,
        new_rm: 'ReconfigurableModule',
        timeout: float = 10.0
    ) -> bool:
        """
        Simple reconfiguration without phase modeling.

        This is a simpler, faster path for when you don't need
        the full phase simulation. Use `reconfigure()` for proper
        PR behavior modeling.

        Parameters
        ----------
        new_rm : ReconfigurableModule
            New RM to load
        timeout : float
            Timeout for graceful termination

        Returns
        -------
        bool
            Success status
        """
        try:
            if self.active_rm is not None:
                self.unload_rm()

            self.load_rm(new_rm)
            return True

        except Exception as e:
            raise PRReconfigurationError(
                f"Failed to reconfigure partition '{self.name}': {e}"
            ) from e

    @property
    def boundary(self) -> PartitionBoundary:
        """Get the partition boundary for isolation control."""
        return self._boundary

    @property
    def reconfig_phase(self) -> ReconfigurationPhase:
        """Get current reconfiguration phase."""
        return self._reconfig_controller.phase

    @property
    def is_isolated(self) -> bool:
        """True if partition boundary is currently isolated."""
        return self._boundary.is_isolated

    @property
    def is_reconfiguring(self) -> bool:
        """True if reconfiguration is in progress."""
        return self._reconfig_controller.is_reconfiguring

    def load_greybox(self) -> bool:
        """
        Load the greybox module into this partition.

        Convenience method for loading the auto-generated greybox.

        Returns
        -------
        bool
            Success status
        """
        if self._greybox_rm is None:
            self.create_greybox()

        return self.load_rm(self._greybox_rm)

    def get_intfs(self) -> Dict:
        """Get persistent interface objects for this partition."""
        return self._intfs

    @staticmethod
    def _normalize_direction(direction: str) -> str:
        """
        Normalize port direction to 'input' or 'output'.

        Accepts: input, in, INPUT, IN, i, output, out, OUTPUT, OUT, o
        """
        if direction is None:
            return 'input'  # Default

        d = direction.lower().strip()
        if d in ('input', 'in', 'i'):
            return 'input'
        elif d in ('output', 'out', 'o'):
            return 'output'
        else:
            raise PRValidationError(
                f"Invalid port direction '{direction}'. "
                f"Expected: input, in, output, out"
            )

    @property
    def umi(self) -> Any:
        """
        Lazy UMI interface - persists across RM swaps.

        This is the key to elegant PR simulation: Python's connection to the
        queue files is independent of which RM process is running. When you
        swap RMs, the queues persist and Python keeps reading/writing the
        same files.

        IMPORTANT: The queue files must be deleted BEFORE this is called.
        Use PRSystem as a context manager to ensure correct lifecycle:

            with PRSystem(config=...) as system:
                system.load('rp0', 'counter_rm')
                umi = system.partitions['rp0'].umi  # Safe - queues pre-deleted

        Raises
        ------
        PRValidationError
            If no UMI ports found or invalid configuration
        """
        if self._umi is None:
            from switchboard import UmiTxRx

            tx_uri = None
            rx_uri = None

            umi_ports_found = False
            for port_name, port_def in self.interface.items():
                port_type = port_def.get('type', 'umi')
                if port_type != 'umi':
                    continue

                umi_ports_found = True
                uri = self._queue_uris[port_name]
                direction = self._normalize_direction(port_def.get('direction'))

                if direction == 'input':
                    if tx_uri is not None:
                        raise PRValidationError(
                            f"Multiple input UMI ports found: already have tx, "
                            f"now found '{port_name}'"
                        )
                    tx_uri = uri
                else:
                    if rx_uri is not None:
                        raise PRValidationError(
                            f"Multiple output UMI ports found: already have rx, "
                            f"now found '{port_name}'"
                        )
                    rx_uri = uri

            if not umi_ports_found:
                raise PRValidationError(
                    f"No UMI ports found in partition '{self.name}' interface. "
                    f"Available ports: {list(self.interface.keys())}"
                )

            if tx_uri is None and rx_uri is None:
                raise PRValidationError(
                    f"No valid UMI ports configured in partition '{self.name}'"
                )

            self._umi = UmiTxRx(tx_uri=tx_uri, rx_uri=rx_uri, fresh=False)
            self._intfs['umi'] = self._umi

        return self._umi

    @property
    def is_loaded(self) -> bool:
        """Check if an RM is currently loaded."""
        return self.active_rm is not None

    @property
    def is_running(self) -> bool:
        """Check if the loaded RM is running."""
        if self.active_rm is None:
            return False
        return self.active_rm.is_running

    def terminate(self, timeout: float = 10.0):
        """Terminate partition (unload RM)."""
        self.unload_rm()

    def get_rm_names(self) -> List[str]:
        """Get names of all registered RMs."""
        return list(self.registered_rms.keys())

    def get_rm(self, name: str) -> Optional['ReconfigurableModule']:
        """Get registered RM by name."""
        return self.registered_rms.get(name)

    def __repr__(self) -> str:
        active = self.active_rm.name if self.active_rm else "none"
        return f"<Partition '{self.name}' active_rm='{active}' registered={len(self.registered_rms)}>"
