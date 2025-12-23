from typing import Dict, List, Optional, Union, Any
from pathlib import Path
import logging

from .config import PRConfig
from .partition import Partition
from .module import ReconfigurableModule
from .validation import PortValidator
from .greybox import GreyboxGenerator
from .timing import ConfigurationTimingModel, ConfigInterface
from .static import StaticRegion
from .reconfiguration import ResetBehavior
from .exceptions import PRConfigError, PRValidationError, PRReconfigurationError, PRBuildError
from .barrier import CycleBarrier

from switchboard.util import ProcessCollection

logger = logging.getLogger(__name__)


class PRSystem:
    """
    Top-level orchestrator for partial reconfiguration simulation.

    PRSystem manages the entire PR simulation lifecycle:
    1. Configuration loading and validation
    2. Building all simulation binaries
    3. Running static region + initial RMs
    4. Runtime reconfiguration of partitions

    Example usage:
        system = PRSystem(config='pr_config.yaml')
        system.build()
        system.simulate()
        system.reconfigure('partition_a', 'rm_accelerator_v2')
        system = PRSystem()
        part = system.add_partition('rp0', interface={...})
        system.add_rm('rm1', 'rp0', design='MyRM', sources=['rm.sv'])
        system.build()
        system.simulate()
    """

    def __init__(
        self,
        config: Union[str, Path, PRConfig, dict] = None,
        tool: str = 'verilator',
        trace: bool = False,
        trace_type: str = 'vcd',
        frequency: float = 100e6,
        max_rate: float = -1,
        start_delay: float = None,
        cmdline: bool = False,
        build_dir: str = None,
        config_timing: bool = False,
        require_static_region: bool = True,
        cycle_accurate: bool = False
    ):
        """
        Initialize PR simulation system.

        Parameters
        ----------
        config : str, Path, PRConfig, or dict, optional
            Configuration file path, PRConfig object, or dict
        tool : str
            Simulation tool ('verilator' or 'icarus')
        trace : bool
            Enable waveform tracing
        trace_type : str
            Trace file format ('vcd' or 'fst')
        frequency : float
            Clock frequency in Hz
        max_rate : float
            Maximum simulation rate (-1 for unlimited)
        start_delay : float
            Delay before starting simulation
        cmdline : bool
            Parse command line arguments
        build_dir : str
            Build directory for simulation artifacts
        config_timing : bool
            Enable configuration timing model.
            When True, each RM must specify config_time_ms.
            When False (default), swaps are instant.
        require_static_region : bool
            If True (default), validation will fail without a static region.
            Set to False only for testing standalone RMs.
        cycle_accurate : bool
            Enable cycle-accurate simulation mode.
            When True, uses barrier synchronization to ensure all processes
            (static region + RMs) advance cycle-by-cycle together.
            WARNING: This is extremely slow (barrier per cycle).
        """
        self.config: Optional[PRConfig] = None
        self.tool = tool
        self.trace = trace
        self.trace_type = trace_type
        self.frequency = frequency
        self.max_rate = max_rate
        self.start_delay = start_delay
        self.build_dir = build_dir or 'build/pr'
        self.require_static_region = require_static_region
        self.cycle_accurate = cycle_accurate
        self._static_region: Optional[StaticRegion] = None
        self.partitions: Dict[str, Partition] = {}
        self.modules: Dict[str, ReconfigurableModule] = {}
        self._ctrl_umi = None
        self._timing_model = ConfigurationTimingModel(enabled=config_timing)
        self.process_collection = ProcessCollection()
        self._built = False
        self._running = False
        self._validated = False
        self._barrier: Optional[CycleBarrier] = None
        self._barrier_uri: Optional[str] = None
        self._greybox_generator = GreyboxGenerator(
            build_dir=f"{self.build_dir}/greybox"
        )
        if cmdline:
            self._parse_cmdline()
        if config is not None:
            self.load_config(config)

    def __enter__(self):
        """
        Initialize queue lifecycle correctly.

        This is the key to eliminating race conditions: we delete all
        queues BEFORE anything connects. Then:
        1. Static region starts and connects to queue files
        2. RMs start and connect to partition-facing queue files
        3. Python interfaces connect with fresh=False to same files
        4. Everyone is talking to the same queues

        Queue architecture with static region:
        - ext_req.q / ext_resp.q: Python ↔ Static Region
        - ctrl_req.q / ctrl_resp.q: Python ↔ Static Region (isolation control)
        - rp0_req.q / rp0_resp.q: Static Region ↔ RM
        """
        from switchboard import delete_queues

        all_uris = []
        if self._static_region is not None:
            for intf_name in self._static_region.interfaces:
                all_uris.append(f"{intf_name}.q")
        for partition in self.partitions.values():
            all_uris.extend(partition._queue_uris.values())
        if all_uris:
            delete_queues(all_uris)
            logger.debug(f"Deleted {len(all_uris)} queues: {all_uris}")

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Clean shutdown."""
        self.terminate()
        return False  # Don't suppress exceptions

    def load(self, partition_name: str, rm_name: str) -> 'PRSystem':
        """
        Load an RM into a partition - builds if needed, starts fresh process.

        This is a convenience method that combines build + load_rm.
        IMPORTANT: Static region is started automatically on first load.

        Parameters
        ----------
        partition_name : str
            Name of the partition to load into
        rm_name : str
            Name of the RM to load

        Returns
        -------
        PRSystem
            Self for method chaining
        """
        if partition_name not in self.partitions:
            raise PRReconfigurationError(f"Unknown partition: '{partition_name}'")
        rm = self.modules.get(rm_name)
        if rm is None:
            partition = self.partitions[partition_name]
            rm = partition.registered_rms.get(rm_name)
        if rm is None:
            raise PRReconfigurationError(f"Unknown RM: '{rm_name}'")
        if not self._built:
            self.build()
        elif not rm.is_built:
            rm.build()
        if self._static_region is not None and not self._static_region.is_running:
            logger.info("Starting static region (runs continuously during simulation)")
            proc = self._static_region.start(start_delay=self.start_delay)
            self.process_collection.add(proc)
        self.partitions[partition_name].load_rm(rm)

        self._running = True
        return self

    def _parse_cmdline(self):
        """Parse command line arguments."""
        from switchboard.cmdline import get_cmdline_args

        args = get_cmdline_args(
            tool=self.tool,
            trace=self.trace,
            trace_type=self.trace_type,
            frequency=self.frequency,
            max_rate=self.max_rate,
            start_delay=self.start_delay
        )

        self.tool = args.tool
        self.trace = args.trace
        self.trace_type = args.trace_type
        self.frequency = args.frequency
        self.max_rate = args.max_rate
        self.start_delay = args.start_delay

    def load_config(self, config: Union[str, Path, PRConfig, dict]) -> 'PRSystem':
        """
        Load PR configuration.

        Parameters
        ----------
        config : str, Path, PRConfig, or dict
            Configuration source

        Returns
        -------
        PRSystem
            Self for method chaining
        """
        if isinstance(config, PRConfig):
            self.config = config
        elif isinstance(config, dict):
            self.config = PRConfig.from_dict(config)
        else:
            self.config = PRConfig.load(config)

        self._setup_from_config()
        return self

    def _setup_from_config(self):
        """Set up components from loaded configuration."""
        if self.config is None:
            return
        sim = self.config.simulation
        if 'tool' in sim:
            self.tool = sim['tool']
        if 'trace' in sim:
            self.trace = sim['trace']
        if 'trace_type' in sim:
            self.trace_type = sim['trace_type']
        if 'frequency' in sim:
            self.frequency = float(sim['frequency'])
        if 'max_rate' in sim:
            self.max_rate = float(sim['max_rate'])
        if 'start_delay' in sim:
            self.start_delay = float(sim['start_delay'])
        if 'build_dir' in sim:
            self.build_dir = sim['build_dir']
        sim = self.config.simulation
        if 'config_timing' in sim:
            self._timing_model = ConfigurationTimingModel(enabled=sim['config_timing'])
        if self.config.static_region:
            self._setup_static_region(self.config.static_region)
        for part_cfg in self.config.partitions:
            if 'boundary' in part_cfg and 'umi_interface' in part_cfg:
                interface = part_cfg['umi_interface']
            elif 'interface' in part_cfg:
                interface = part_cfg['interface']
            else:
                interface = {
                    'req': {'type': 'umi', 'direction': 'input', 'dw': 256, 'aw': 64, 'cw': 32},
                    'resp': {'type': 'umi', 'direction': 'output', 'dw': 256, 'aw': 64, 'cw': 32}
                }

            partition = Partition(
                name=part_cfg['name'],
                interface=interface,
                system=self,
                greybox=part_cfg.get('greybox', False),
                initial_rm=part_cfg.get('initial_rm')
            )
            self.partitions[part_cfg['name']] = partition
        for rm_cfg in self.config.reconfigurable_modules:
            rm = ReconfigurableModule(
                name=rm_cfg['name'],
                partition_name=rm_cfg['partition'],
                design=rm_cfg.get('design'),
                sources=rm_cfg.get('sources', []),
                parameters=rm_cfg.get('parameters', {}),
                port_mapping=rm_cfg.get('port_mapping', {}),
                port_compatibility=rm_cfg.get('port_compatibility', {}),
                clocks=rm_cfg.get('clocks', ['clk']),
                resets=rm_cfg.get('resets', []),
                tieoffs=rm_cfg.get('tieoffs', {}),
                interfaces=rm_cfg.get('interfaces', {}),
                system=self,
                auto_wrap=rm_cfg.get('auto_wrap', False),
                auto_wrap_config=rm_cfg.get('auto_wrap_config', {}),
                ports_override=rm_cfg.get('ports')
            )
            self.modules[rm_cfg['name']] = rm
            partition = self.partitions[rm_cfg['partition']]
            partition.register_rm(rm)
        for partition in self.partitions.values():
            if partition.enable_greybox:
                greybox = partition.create_greybox()
                self.modules[greybox.name] = greybox

    def _setup_static_region(self, sr_cfg: Dict):
        """Set up static region from configuration."""
        self._static_region = StaticRegion(
            name=sr_cfg.get('name', 'static_region'),
            design=sr_cfg.get('design'),
            sources=sr_cfg.get('sources', []),
            parameters=sr_cfg.get('parameters', {}),
            interfaces=sr_cfg.get('interfaces', {}),
            clocks=sr_cfg.get('clocks', ['clk']),
            resets=sr_cfg.get('resets', []),
            system=self,
            build_dir=f"{self.build_dir}/static",
            auto_wrap=sr_cfg.get('auto_wrap', False),
            auto_wrap_config=sr_cfg.get('auto_wrap_config', {}),
            ports_override=sr_cfg.get('ports')
        )

    @property
    def static_region(self) -> Optional[StaticRegion]:
        """Get the static region (if defined)."""
        return self._static_region

    @property
    def timing_model(self) -> ConfigurationTimingModel:
        """Get the configuration timing model."""
        return self._timing_model

    @property
    def config_timing_enabled(self) -> bool:
        """True if configuration timing is enabled."""
        return self._timing_model.enabled

    def enable_config_timing(self, enabled: bool = True):
        """Enable or disable configuration timing model."""
        self._timing_model.enabled = enabled

    def add_partition(
        self,
        name: str,
        interface: Dict[str, Dict],
        greybox: bool = False,
        initial_rm: str = None
    ) -> Partition:
        """
        Add a partition programmatically.

        Parameters
        ----------
        name : str
            Partition name
        interface : dict
            Interface definition
            Format: {port_name: {type, direction, dw, ...}}
        greybox : bool
            Auto-generate greybox module
        initial_rm : str
            Initial RM to load

        Returns
        -------
        Partition
            Created partition
        """
        partition = Partition(
            name=name,
            interface=interface,
            system=self,
            greybox=greybox,
            initial_rm=initial_rm
        )
        self.partitions[name] = partition
        if greybox:
            gb = partition.create_greybox()
            self.modules[gb.name] = gb

        return partition

    def add_rm(
        self,
        name: str,
        partition: str,
        design: str = None,
        sources: List[str] = None,
        parameters: Dict = None,
        port_mapping: Dict[str, str] = None,
        clocks: List[str] = None,
        resets: List = None,
        tieoffs: Dict = None
    ) -> ReconfigurableModule:
        """
        Add a reconfigurable module programmatically.

        Parameters
        ----------
        name : str
            Module name
        partition : str
            Target partition name
        design : str
            Design/module name
        sources : list
            Source file paths
        parameters : dict
            Module parameters
        port_mapping : dict
            Partition port to RM port mapping
        clocks : list
            Clock signals
        resets : list
            Reset signals
        tieoffs : dict
            Tie-off configurations

        Returns
        -------
        ReconfigurableModule
            Created module
        """
        if partition not in self.partitions:
            raise PRConfigError(f"Partition '{partition}' does not exist")

        rm = ReconfigurableModule(
            name=name,
            partition_name=partition,
            design=design,
            sources=sources or [],
            parameters=parameters or {},
            port_mapping=port_mapping or {},
            clocks=clocks or ['clk'],
            resets=resets or [],
            tieoffs=tieoffs or {},
            system=self
        )
        self.modules[name] = rm
        self.partitions[partition].register_rm(rm)

        return rm

    def validate(self) -> bool:
        """
        Validate all configurations and port compatibility.

        Returns
        -------
        bool
            True if validation passes

        Raises
        ------
        PRValidationError
            If validation fails
        """
        if self.require_static_region and self._static_region is None:
            raise PRValidationError(
                "Static region is REQUIRED for proper PR simulation.\n"
                "The static region represents the persistent logic that keeps running "
                "while RMs swap.\n"
                "Without it, you're just swapping independent processes, not modeling real PR.\n"
                "\n"
                "To fix this, add a 'static_region' section to your config:\n"
                "  static_region:\n"
                "    name: my_static\n"
                "    sources: [rtl/static_region.sv]\n"
                "\n"
                "Or set require_static_region=False if you really want standalone RM testing."
            )
        policy = 'superset'  # Default
        if self.config and self.config.port_compatibility_rules:
            policy = self.config.port_compatibility_rules.get('default_policy', 'superset')

        validator = PortValidator(policy=policy)

        for partition in self.partitions.values():
            validator.validate_partition(partition)

        self._validated = True
        return True

    def build(self, fast: bool = False) -> 'PRSystem':
        """
        Build all simulations (static region + all RMs).

        For partitions with boundary definitions, this:
        1. Generates queue bridges for the static region side
        2. Generates queue wrappers for the RM side
        3. Connects them via Switchboard queues

        Parameters
        ----------
        fast : bool
            Skip rebuild if binary exists

        Returns
        -------
        PRSystem
            Self for method chaining
        """
        if not self._validated:
            self.validate()
        self._generate_partition_bridges()
        if self._static_region is not None:
            self._static_region.build(fast=fast)
        for rm in self.modules.values():
            rm.build(fast=fast)

        self._built = True
        return self

    def _generate_partition_bridges(self):
        """
        Generate queue bridges for partitions with boundary definitions.

        For each partition with a 'boundary' in config:
        1. Generate static-side queue bridge (replaces RM module in static region)
        2. Store bridge info for RM-side wrapper generation

        The static-side bridge has the same module name as the RM, so when
        static region is compiled, it uses the bridge instead of the real RM.
        """
        if self.config is None:
            return

        from .queue_bridge import QueueBridgeGenerator, PartitionBoundary, PartitionBoundaryPort

        queue_dir = Path(self.build_dir).resolve() / 'queues'
        bridge_gen = QueueBridgeGenerator(
            build_dir=str(Path(self.build_dir) / 'bridges'),
            queue_dir=str(queue_dir)
        )

        for part_cfg in self.config.partitions:
            if 'boundary' not in part_cfg:
                continue

            partition_name = part_cfg['name']
            rm_module = part_cfg.get('rm_module')
            clock_name = part_cfg.get('clock', 'clk')

            if not rm_module:
                logger.warning(
                    f"Partition '{partition_name}' has boundary but no rm_module specified"
                )
                continue
            ports = []
            for port_cfg in part_cfg['boundary']:
                ports.append(PartitionBoundaryPort(
                    name=port_cfg['name'],
                    width=port_cfg.get('width', 1),
                    direction=port_cfg['direction']
                ))

            boundary = PartitionBoundary(
                partition_name=partition_name,
                rm_module_name=rm_module,
                ports=ports,
                clock_name=clock_name
            )
            bridge_path = bridge_gen.generate_static_side_bridge(
                boundary=boundary,
                queue_prefix=partition_name
            )
            logger.info(f"Generated static-side bridge for partition '{partition_name}': {bridge_path}")
            partition = self.partitions.get(partition_name)
            if partition:
                partition._boundary_config = boundary
                partition._bridge_path = bridge_path
            if self._static_region is not None:
                self._static_region.sources.append(str(bridge_path))
                logger.info(f"Added bridge to static region sources: {bridge_path}")

    def simulate(
        self,
        initial_rms: Dict[str, str] = None,
        start_delay: float = None
    ) -> ProcessCollection:
        """
        Start the simulation with initial RMs.

        The static region (if defined) starts FIRST and keeps running
        for the entire simulation. RMs then connect to the static region
        via partition boundaries.

        Parameters
        ----------
        initial_rms : dict, optional
            Mapping of partition name to initial RM name
            Overrides config settings
        start_delay : float, optional
            Delay before starting

        Returns
        -------
        ProcessCollection
            Collection of running processes
        """
        if not self._built:
            self.build()
        if initial_rms is None:
            initial_rms = {}
        for part_name, partition in self.partitions.items():
            if part_name not in initial_rms and partition.initial_rm_name:
                initial_rms[part_name] = partition.initial_rm_name
        barrier_plusargs = []
        if self.cycle_accurate:
            num_procs = (1 if self._static_region is not None else 0) + len(initial_rms)
            if num_procs < 2:
                logger.warning(
                    "cycle_accurate=True but fewer than 2 processes - "
                    "barrier sync requires static region + at least one RM"
                )
            else:
                barrier_dir = Path(self.build_dir) / 'barrier'
                barrier_dir.mkdir(parents=True, exist_ok=True)
                self._barrier_uri = str(barrier_dir / 'cycle_barrier.sync')

                self._barrier = CycleBarrier(
                    uri=self._barrier_uri,
                    create=True,
                    num_processes=num_procs
                )
                logger.info(
                    f"Created cycle barrier for {num_procs} processes: {self._barrier_uri}"
                )

        if self._static_region is not None:
            if self.cycle_accurate and self._barrier is not None:
                barrier_plusargs = [
                    f'barrier_uri={self._barrier_uri}',
                    'barrier_leader=0',
                    f'barrier_procs={self._barrier.get_num_processes()}'
                ]
            proc = self._static_region.start(
                start_delay=start_delay or self.start_delay,
                extra_plusargs=barrier_plusargs
            )
            self.process_collection.add(proc)
            logger.info("Static region started - will keep running during RM swaps")

        for partition_name, rm_name in initial_rms.items():
            if partition_name not in self.partitions:
                raise PRReconfigurationError(
                    f"Unknown partition: '{partition_name}'"
                )
            if rm_name not in self.modules:
                raise PRReconfigurationError(
                    f"Unknown RM: '{rm_name}'"
                )

            partition = self.partitions[partition_name]
            rm = self.modules[rm_name]
            rm_barrier_plusargs = []
            if self.cycle_accurate and self._barrier is not None:
                rm_barrier_plusargs = [
                    f'barrier_uri={self._barrier_uri}',
                    'barrier_leader=0',
                    f'barrier_procs={self._barrier.get_num_processes()}'
                ]

            partition.load_rm(rm, extra_plusargs=rm_barrier_plusargs)

        self._running = True
        return self.process_collection

    def reconfigure(
        self,
        partition: str,
        new_rm: str,
        timeout: float = 10.0
    ) -> bool:
        """
        Reconfigure a partition with a new RM.

        This is the main reconfiguration API. It follows proper PR phases:
        1. ISOLATE - Set hardware isolation via static region control
        2. SWAP - Terminate current RM, start new RM (fresh state)
        3. RELEASE - Clear hardware isolation

        The new RM starts with reset register values - this matches
        real FPGA PR behavior (GSR on Xilinx, manual reset on Intel).

        Parameters
        ----------
        partition : str
            Partition name to reconfigure
        new_rm : str
            Name of new RM to load
        timeout : float
            Timeout for graceful termination

        Returns
        -------
        bool
            True if reconfiguration succeeded

        Raises
        ------
        PRReconfigurationError
            If reconfiguration fails
        """
        if partition not in self.partitions:
            raise PRReconfigurationError(f"Unknown partition: '{partition}'")
        if new_rm not in self.modules:
            raise PRReconfigurationError(f"Unknown RM: '{new_rm}'")

        part = self.partitions[partition]
        rm = self.modules[new_rm]
        if self._static_region is not None:
            logger.info(f"Setting hardware isolation for partition '{partition}'")
            self.set_isolation(partition, isolated=True)

        try:
            result = part.reconfigure(new_rm=rm, timeout=timeout)
            if self._static_region is not None:
                logger.info(f"Releasing hardware isolation for partition '{partition}'")
                self.set_isolation(partition, isolated=False)

            return result

        except Exception as e:
            if self._static_region is not None:
                try:
                    self.set_isolation(partition, isolated=False)
                except Exception:
                    pass
            raise

    def load_greybox(self, partition: str) -> bool:
        """
        Load greybox module into a partition.

        Parameters
        ----------
        partition : str
            Partition name

        Returns
        -------
        bool
            Success status
        """
        if partition not in self.partitions:
            raise PRReconfigurationError(f"Unknown partition: '{partition}'")

        return self.partitions[partition].load_greybox()

    def get_partition(self, name: str) -> Optional[Partition]:
        """Get partition by name."""
        return self.partitions.get(name)

    def get_rm(self, name: str) -> Optional[ReconfigurableModule]:
        """Get reconfigurable module by name."""
        return self.modules.get(name)

    def get_rm_api(self, partition_name: str, umi=None):
        """
        Get the auto-generated Python API for the currently active RM in a partition.

        This is a convenience method that:
        1. Gets the currently active RM in the partition
        2. Creates a UMI interface to the partition if not provided
        3. Returns the RM's auto-generated API

        Parameters
        ----------
        partition_name : str
            Name of the partition
        umi : UmiTxRx, optional
            UMI interface to use. If not provided, creates one via umi_for_partition()

        Returns
        -------
        object
            Instance of the auto-generated API class for the active RM

        Raises
        ------
        PRReconfigurationError
            If partition not found or no active RM

        Example
        -------
        ```python
        system = PRSystem(config='pr_config.yaml')
        system.build()
        system.simulate()
        api = system.get_rm_api('rp0')
        api.write_counter(0x12345678)
        led_state = api.read_led_status()
        system.reconfigure('rp0', 'new_rm')
        api = system.get_rm_api('rp0')  # Returns API for new RM
        ```
        """
        if partition_name not in self.partitions:
            raise PRReconfigurationError(f"Unknown partition: '{partition_name}'")

        partition = self.partitions[partition_name]
        active_rm = partition.active_rm
        if active_rm is None:
            raise PRReconfigurationError(
                f"No active RM in partition '{partition_name}'"
            )

        if not active_rm.auto_wrap:
            raise PRReconfigurationError(
                f"RM '{active_rm.name}' does not have auto_wrap enabled. "
                f"Set auto_wrap: true in the RM configuration."
            )
        if umi is None:
            umi = self.umi_for_partition(partition_name)

        return active_rm.get_api(umi)

    @property
    def intfs(self) -> Dict:
        """Get all interface objects for Python interaction."""
        result = {}
        if self.static_region and hasattr(self.static_region, 'intfs'):
            result.update(self.static_region.intfs)
        for partition in self.partitions.values():
            result.update(partition.get_intfs())

        return result

    def _get_partition_mapping(self, partition_name: str) -> Optional[Dict]:
        """
        Get partition mapping from static region config.

        Returns the mapping dict with keys:
        - external_req: queue name for external request interface
        - external_resp: queue name for external response interface
        - partition_req: queue name for partition request interface
        - partition_resp: queue name for partition response interface
        - isolation_bit: bit index in isolation control register

        Returns None if no mapping exists.
        """
        if self._static_region is None or self.config is None:
            return None

        sr_cfg = self.config.static_region
        if not sr_cfg:
            return None

        return sr_cfg.get('partition_mapping', {}).get(partition_name)

    def umi_for_partition(self, partition_name: str) -> Any:
        """
        Get UMI interface for a partition, routed through static region.

        When a static region is present, traffic flows:
        Python → Static Region → RM

        The mapping is derived from static_region.interfaces using naming
        conventions (see config.py:_derive_partition_mapping) or can be
        explicitly specified in the config.

        Parameters
        ----------
        partition_name : str
            Name of partition to get UMI for

        Returns
        -------
        UmiTxRx
            UMI interface that talks through static region
        """
        if partition_name not in self.partitions:
            raise PRReconfigurationError(f"Unknown partition: '{partition_name}'")
        cache_key = f'_umi_{partition_name}'
        if hasattr(self, cache_key):
            cached = getattr(self, cache_key)
            if cached is not None:
                return cached

        from switchboard import UmiTxRx
        if self._static_region is not None:
            mapping = self._get_partition_mapping(partition_name)
            if mapping:
                tx_uri = f"{mapping['external_req']}.q"
                rx_uri = f"{mapping['external_resp']}.q"
            else:
                partition = self.partitions[partition_name]
                tx_uri = partition.get_queue_uri('req')
                rx_uri = partition.get_queue_uri('resp')
                logger.debug(
                    f"No partition mapping for '{partition_name}' - using direct partition queues: "
                    f"tx={tx_uri}, rx={rx_uri}"
                )
        else:
            partition = self.partitions[partition_name]
            tx_uri = partition.get_queue_uri('req')
            rx_uri = partition.get_queue_uri('resp')

        umi = UmiTxRx(tx_uri=tx_uri, rx_uri=rx_uri, fresh=False)
        setattr(self, cache_key, umi)
        return umi

    def umi_for_static(self) -> Any:
        """
        Get UMI interface for the static region.

        This returns a UmiTxRx interface that can communicate directly with
        the static region's UMI wrapper. The static region must be started
        (running) for this to work.

        Returns
        -------
        UmiTxRx
            UMI interface that talks to the static region

        Raises
        ------
        PRReconfigurationError
            If no static region exists, not running, or no UMI interface available

        Example
        -------
        ```python
        system = PRSystem(config='pr_config.yaml')
        system.build()
        system.simulate()
        static_umi = system.umi_for_static()
        value = static_umi.read(0x0, np.uint32)
        ```
        """
        if self._static_region is None:
            raise PRReconfigurationError(
                "No static region configured - cannot get UMI interface"
            )

        if not self._static_region._running:
            raise PRReconfigurationError(
                "Static region is not running - call simulate() first"
            )
        if hasattr(self, '_umi_static') and self._umi_static is not None:
            return self._umi_static
        if self._static_region._intfs:
            for intf_name, intf in self._static_region._intfs.items():
                if hasattr(intf, 'read') and hasattr(intf, 'write'):
                    self._umi_static = intf
                    return intf
        from switchboard import UmiTxRx
        tx_uri = 'req.q'
        rx_uri = 'resp.q'

        self._umi_static = UmiTxRx(tx_uri=tx_uri, rx_uri=rx_uri, fresh=False)
        return self._umi_static

    def get_static_api(self, umi=None):
        """
        Get the auto-generated Python API for the static region.

        This is a convenience method that:
        1. Gets or creates a UMI interface to the static region
        2. Returns the static region's auto-generated API

        Parameters
        ----------
        umi : UmiTxRx, optional
            UMI interface to use. If not provided, creates one via umi_for_static()

        Returns
        -------
        object
            Instance of the auto-generated API class for the static region

        Raises
        ------
        PRReconfigurationError
            If no static region exists or auto_wrap is not enabled

        Example
        -------
        ```python
        system = PRSystem(config='pr_config.yaml')
        system.build()
        system.simulate()
        static_api = system.get_static_api()
        counter = static_api.read_activity_counter()
        print(f"Activity counter: {counter:,}")
        ```
        """
        if self._static_region is None:
            raise PRReconfigurationError(
                "No static region configured - cannot get API"
            )

        if not self._static_region.auto_wrap:
            raise PRReconfigurationError(
                f"Static region '{self._static_region.name}' does not have auto_wrap enabled. "
                f"Set auto_wrap: true in the static_region configuration."
            )

        if not self._static_region._running:
            raise PRReconfigurationError(
                "Static region is not running - call simulate() first"
            )
        if umi is None:
            umi = self.umi_for_static()

        return self._static_region.get_api(umi)

    @property
    def ctrl_umi(self) -> Any:
        """
        Get UMI interface for static region control (isolation control).

        Write to address 0x0 to set isolation bits:
        - Bit 0: rp0 isolation
        - Bit 1: rp1 isolation (if present)

        Returns
        -------
        UmiTxRx
            UMI interface for control
        """
        if self._ctrl_umi is None:
            if self._static_region is None:
                raise PRReconfigurationError(
                    "No static region - cannot get control interface"
                )

            from switchboard import UmiTxRx
            self._ctrl_umi = UmiTxRx(
                tx_uri='ctrl_req.q',
                rx_uri='ctrl_resp.q',
                fresh=False
            )

        return self._ctrl_umi

    def set_isolation(self, partition_name: str, isolated: bool):
        """
        Set isolation state for a partition via static region control.

        The isolation bit index is derived from the partition mapping in config,
        which is auto-generated by config.py:_derive_partition_mapping() or
        can be explicitly specified.

        Parameters
        ----------
        partition_name : str
            Partition to isolate/release
        isolated : bool
            True to isolate, False to release
        """
        import numpy as np

        if self._static_region is None:
            logger.warning("No static region - isolation is software-only")
            return
        partition = self.partitions.get(partition_name)
        if partition and hasattr(partition, '_boundary_config') and partition._boundary_config:
            logger.debug(
                f"Partition '{partition_name}' uses queue-based boundary - "
                f"isolation handled by queue management"
            )
            return
        mapping = self._get_partition_mapping(partition_name)
        if not mapping:
            logger.warning(
                f"No partition mapping for '{partition_name}' - cannot set isolation. "
                f"Ensure static_region.interfaces follows naming convention or "
                f"explicitly define partition_mapping in config."
            )
            return

        bit_idx = mapping.get('isolation_bit')
        if bit_idx is None:
            logger.warning(f"No isolation_bit defined for partition '{partition_name}'")
            return
        ctrl = self.ctrl_umi
        current = ctrl.read(0x0, np.uint32)
        bit_mask = np.uint32(1 << bit_idx)
        if isolated:
            new_val = current | bit_mask
        else:
            new_val = current & ~bit_mask  # ~uint32 stays in 32-bit space
        ctrl.write(0x0, np.uint32(new_val))
        logger.debug(f"Set isolation for {partition_name}: {isolated} (bit {bit_idx}, bits: {new_val:#x})")

    def terminate(self, timeout: float = 10.0):
        """Terminate all simulations."""
        for partition in self.partitions.values():
            partition.terminate(timeout=timeout)
        if self._static_region is not None:
            self._static_region.terminate(timeout=timeout)
        self.process_collection.terminate(stop_timeout=timeout)

        # Close barrier if present
        if self._barrier is not None:
            self._barrier.close()
            self._barrier = None
            self._barrier_uri = None

        self._running = False
        logger.info("All simulations terminated")

    def wait(self, timeout: float = None):
        """Wait for all simulations to complete."""
        self.process_collection.wait()

    @property
    def is_running(self) -> bool:
        """Check if simulation is running."""
        return self._running

    @property
    def is_built(self) -> bool:
        """Check if all components are built."""
        return self._built

    def __repr__(self) -> str:
        return (
            f"<PRSystem partitions={len(self.partitions)} "
            f"modules={len(self.modules)} "
            f"running={self._running}>"
        )
