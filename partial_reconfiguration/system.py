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
from .verilator_builder import VerilatorBuilder
from .sim_process import SimulationProcessManager
from .shm_interface import SharedMemoryInterface

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
        self._ctrl_shm = None
        self._timing_model = ConfigurationTimingModel(enabled=config_timing)
        self._sim_process: Optional[SimulationProcessManager] = None
        self._builder: Optional[VerilatorBuilder] = None
        self._shm_interfaces: Dict[str, SharedMemoryInterface] = {}
        self._binary_paths: Dict[str, Path] = {}  # 'static', 'rm/name' -> Path
        self._rm_binary_map: Dict[str, str] = {}  # rm_name -> binary_path
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
        Initialize simulation lifecycle.

        Sets up shared memory for multi-process communication.
        DPI channels use mmap'd shared memory
        between static binary and RM binaries.
        """
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
        if not self._running:
            self.simulate(initial_rms={partition_name: rm_name})
        else:
            self.partitions[partition_name].load_rm(rm)

        self._running = True
        return self

    def _parse_cmdline(self):
        """Parse command line arguments."""
        import argparse

        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument('--tool', default=self.tool)
        parser.add_argument('--trace', action='store_true', default=self.trace)
        parser.add_argument('--trace-type', default=self.trace_type)
        parser.add_argument('--frequency', type=float, default=self.frequency)
        parser.add_argument('--max-rate', type=float, default=self.max_rate)
        parser.add_argument('--start-delay', type=float, default=self.start_delay)

        args, _ = parser.parse_known_args()

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
            if 'interface' in part_cfg:
                interface = part_cfg['interface']
            else:
                interface = {}

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
        # Support both 'clock' (singular) and 'clocks' (list) config keys.
        # pyslang auto-detects clock from RTL in _setup_builder(), so this
        # is only a fallback for when pyslang isn't available.
        if 'clock' in sr_cfg:
            clocks = [sr_cfg['clock']]
        else:
            clocks = sr_cfg.get('clocks', ['clk'])

        self._static_region = StaticRegion(
            name=sr_cfg.get('name', 'static_region'),
            design=sr_cfg.get('design'),
            sources=sr_cfg.get('sources', []),
            parameters=sr_cfg.get('parameters', {}),
            interfaces=sr_cfg.get('interfaces', {}),
            clocks=clocks,
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
        Build all simulation binaries (multi-binary architecture).

        Uses VerilatorBuilder to:
        1. Generate DPI bridges for each partition boundary
        2. Generate DPI C++ code (static_driver, rm_drivers, channels)
        3. Verilate all modules (static + RM wrappers)
        4. Compile and link into N+1 simulation binaries

        Parameters
        ----------
        fast : bool
            Skip rebuild if static_binary exists

        Returns
        -------
        PRSystem
            Self for method chaining
        """
        if not self._validated:
            self.validate()

        static_binary_path = Path(self.build_dir) / 'static_binary'
        if fast and static_binary_path.exists():
            logger.info("Skipping build (fast=True and static_binary exists)")
            self._built = True
            # Reconstruct binary paths from filesystem
            self._binary_paths = {'static': static_binary_path}
            for part_name, partition in self.partitions.items():
                for rm_name, rm in partition.registered_rms.items():
                    rm_path = Path(self.build_dir) / 'rm' / rm_name / 'rm_binary'
                    if rm_path.exists():
                        self._binary_paths[f'rm/{rm_name}'] = rm_path
                        self._rm_binary_map[rm_name] = str(rm_path)
                    if not rm._built:
                        rm.build()
            return self

        self._builder = VerilatorBuilder(
            build_dir=self.build_dir,
            trace=self.trace,
            trace_type=self.trace_type,
        )

        self._setup_builder()
        self._binary_paths = self._builder.build()

        # Build static region and RMs AFTER builder (generates API classes
        # for get_api()). Must be after _setup_builder() which populates
        # _static_ports_for_api used by _generate_api().
        if self._static_region and not self._static_region._built:
            self._static_region.build()
        for partition in self.partitions.values():
            for rm in partition.registered_rms.values():
                if not rm._built:
                    rm.build()

        # Build rm_name -> binary_path lookup
        for key, path in self._binary_paths.items():
            if key.startswith('rm/'):
                rm_name = key[3:]  # strip 'rm/' prefix
                self._rm_binary_map[rm_name] = str(path)

        self._built = True
        return self

    def _setup_builder(self):
        """Configure the VerilatorBuilder from system config."""
        if self._builder is None:
            raise PRBuildError("Builder not initialized")

        if self._static_region is None:
            raise PRBuildError("No static region configured")

        # Resolve static region sources
        static_sources = self._resolve_sources(self._static_region.sources)
        static_design = self._static_region.design if isinstance(
            self._static_region.design, str
        ) else self._static_region.name

        # Collect static region ports for signal access
        static_ports = []
        if self._static_region.ports_override:
            for port_name, port_def in self._static_region.ports_override.items():
                static_ports.append({
                    'name': port_name,
                    'width': port_def.get('width', 1),
                    'direction': port_def.get('direction', 'input'),
                })
        elif self._static_region._module_info:
            # Use already-parsed port info
            for port_name, port in self._static_region._module_info.ports.items():
                if port_name in ('clk', 'rst', 'reset', 'rst_n', 'reset_n'):
                    continue
                static_ports.append({
                    'name': port_name,
                    'width': port.width,
                    'direction': port.direction,
                })
        elif self._static_region.sources:
            # Parse RTL directly for port info
            from .rtl_parser import RTLParser
            parser = RTLParser()
            module_name = self._static_region.design if isinstance(
                self._static_region.design, str
            ) else self._static_region.name
            resolved_sources = self._resolve_sources(self._static_region.sources)
            try:
                module_info = parser.parse_module(resolved_sources, module_name)
                for port_name, port in module_info.ports.items():
                    if port_name in ('clk', 'rst', 'reset', 'rst_n', 'reset_n'):
                        continue
                    static_ports.append({
                        'name': port_name,
                        'width': port.width,
                        'direction': port.direction,
                    })
            except Exception as e:
                logger.warning(f"Could not parse static region RTL for port info: {e}")

        # Store static ports for API generation (used by static region's _generate_api)
        from .codegen.api_generator import PortSpec
        self._static_region._static_ports_for_api = [
            PortSpec(
                name=p['name'],
                width=p['width'],
                direction=p['direction'],
                index=idx,
            )
            for idx, p in enumerate(static_ports)
        ]

        # Detect clock name: pyslang RTL analysis > config > default 'clk'
        from .verilator_builder import _detect_clock_from_rtl
        static_clock = _detect_clock_from_rtl(static_sources, static_design)
        if static_clock is None:
            static_clock = self._static_region.clocks[0] if self._static_region.clocks else 'clk'

        self._builder.set_static_region(
            design_name=static_design,
            sources=static_sources,
            ports=static_ports,
            clock_name=static_clock,
        )

        # Register partitions
        if self.config is None:
            return

        for part_idx, part_cfg in enumerate(self.config.partitions):
            if 'boundary' not in part_cfg:
                continue

            partition_name = part_cfg['name']
            rm_module = part_cfg.get('rm_module')
            clock_names = part_cfg.get('clocks', [part_cfg.get('clock', 'clk')])

            # Reset config from partition
            resets = part_cfg.get('resets', [])
            reset_name = resets[0]['name'] if resets else None
            reset_polarity = resets[0].get('polarity', 'negative') if resets else 'negative'
            reset_cycles = part_cfg.get('reset_cycles', 10)
            reset_behavior = part_cfg.get('reset_behavior', 'fresh')

            if not rm_module:
                logger.warning(
                    f"Partition '{partition_name}' has boundary but no rm_module specified"
                )
                continue

            to_rm_ports = []
            from_rm_ports = []
            for port_cfg in part_cfg['boundary']:
                port_dict = {'name': port_cfg['name'], 'width': port_cfg.get('width', 1)}
                if port_cfg['direction'] == 'to_rm':
                    to_rm_ports.append(port_dict)
                else:
                    from_rm_ports.append(port_dict)

            # Collect RM variants for this partition
            partition = self.partitions.get(partition_name)
            rm_variants = []
            if partition:
                for rm_idx, (rm_name, rm) in enumerate(partition.registered_rms.items()):
                    rm_design = rm.design or rm_name
                    rm_sources = self._resolve_sources(rm.sources)
                    rm_variants.append({
                        'name': rm_name,
                        'design': rm_design,
                        'wrapper_name': f"{rm_design}_dpi_wrapper",
                        'index': rm_idx,
                        'sources': rm_sources,
                        'include_dirs': [],
                    })
                    # Store the rm_index on the module for later reference
                    rm._rm_index = rm_idx

            initial_rm_index = 0
            if partition and partition.initial_rm_name:
                for i, rm_name in enumerate(partition.registered_rms.keys()):
                    if rm_name == partition.initial_rm_name:
                        initial_rm_index = i
                        break

            self._builder.add_partition(
                name=partition_name,
                index=part_idx,
                rm_module_name=rm_module,
                clock_names=clock_names,
                to_rm_ports=to_rm_ports,
                from_rm_ports=from_rm_ports,
                rm_variants=rm_variants,
                initial_rm_index=initial_rm_index,
                reset_name=reset_name,
                reset_polarity=reset_polarity,
                reset_cycles=reset_cycles,
                reset_behavior=reset_behavior,
            )

            # Store partition index for shared memory targeting
            if partition:
                partition._partition_index = part_idx

    def _resolve_sources(self, sources: List[str]) -> List[str]:
        """Resolve source file paths relative to config or cwd."""
        resolved = []
        for source in sources:
            source_path = Path(source)
            if source_path.exists():
                resolved.append(str(source_path.resolve()))
            elif self.config and self.config._source_path:
                config_dir = self.config._source_path.parent
                rel_path = config_dir / source
                if rel_path.exists():
                    resolved.append(str(rel_path.resolve()))
                else:
                    resolved.append(source)
            else:
                resolved.append(source)
        return resolved

    def simulate(
        self,
        initial_rms: Dict[str, str] = None,
        start_delay: float = None
    ) -> SimulationProcessManager:
        """
        Start the multi-process simulation with initial RMs.

        Launches N+1 separate processes:
        - 1 static binary (persistent)
        - N RM binaries (one per partition, can be killed/restarted)

        Communication uses mmap'd shared memory:
        - Python <-> static: command mailbox
        - static <-> RM: per-partition DPI channels
        - All processes: barrier for cycle synchronization

        Parameters
        ----------
        initial_rms : dict, optional
            Mapping of partition name to initial RM name
            Overrides config settings
        start_delay : float, optional
            Delay before starting (unused in DPI mode)

        Returns
        -------
        SimulationProcessManager
            The running simulation process manager
        """
        if not self._built:
            self.build()
        if initial_rms is None:
            initial_rms = {}
        for part_name, partition in self.partitions.items():
            if part_name not in initial_rms and partition.initial_rm_name:
                initial_rms[part_name] = partition.initial_rm_name

        # Build partition configs for the process manager
        partition_configs = []
        for part_name, partition in self.partitions.items():
            part_idx = getattr(partition, '_partition_index', 0)
            # Count to_rm and from_rm SLOTS (not ports) from the builder partition info
            # A port of width W occupies ceil(W/64) slots.
            num_to_rm = 0
            num_from_rm = 0
            if self._builder:
                for pi in self._builder._partitions:
                    if pi.name == part_name:
                        num_to_rm = sum((p.get('width', 32) + 63) // 64 for p in pi.to_rm_ports)
                        num_from_rm = sum((p.get('width', 32) + 63) // 64 for p in pi.from_rm_ports)
                        break
            partition_configs.append({
                'name': part_name,
                'index': part_idx,
                'num_to_rm': num_to_rm,
                'num_from_rm': num_from_rm,
            })

        # Validate initial RMs
        for partition_name, rm_name in initial_rms.items():
            if partition_name not in self.partitions:
                raise PRReconfigurationError(
                    f"Unknown partition: '{partition_name}'"
                )
            if rm_name not in self.modules and rm_name not in self._rm_binary_map:
                raise PRReconfigurationError(
                    f"Unknown RM: '{rm_name}'"
                )

        # Start the multi-process simulation
        static_binary = str(self._binary_paths.get('static', Path(self.build_dir) / 'static_binary'))

        self._sim_process = SimulationProcessManager(build_dir=self.build_dir)
        self._sim_process.start(
            static_binary=static_binary,
            rm_binaries=self._rm_binary_map,
            partition_configs=partition_configs,
            initial_rm_map=initial_rms,
        )

        # Mark partitions as having their initial RMs loaded
        for partition_name, rm_name in initial_rms.items():
            partition = self.partitions[partition_name]
            rm = self.modules.get(rm_name)
            if rm:
                partition.active_rm = rm

        self._running = True
        if self._static_region:
            self._static_region._running = True
        logger.info(
            f"Simulation started: {1 + len(initial_rms)} processes "
            f"({len(initial_rms)} partitions)"
        )
        return self._sim_process

    def reconfigure(
        self,
        partition: str,
        new_rm: str,
        timeout: float = 10.0
    ) -> bool:
        """
        Reconfigure a partition with a new RM.

        In the multi-binary architecture, this:
        1. Sends CMD_RECONFIG to the static binary via shared memory mailbox
        2. Static binary sets quit flag on partition channel
        3. Old RM process sees quit and exits
        4. New RM binary is started (connects to same partition channel)
        5. New RM sets rm_ready -> static resumes barrier cycling
        6. Static sets CMD_NOOP -> Python sees completion

        The new RM starts with reset register values - this matches
        real FPGA PR behavior (GSR on Xilinx, manual reset on Intel).

        Parameters
        ----------
        partition : str
            Partition name to reconfigure
        new_rm : str
            Name of new RM to load
        timeout : float
            Timeout for reconfiguration

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
        if new_rm not in self._rm_binary_map:
            raise PRReconfigurationError(
                f"RM '{new_rm}' has no binary path - was the system built?"
            )

        part = self.partitions[partition]
        rm = self.modules[new_rm]
        old_name = part.active_rm.name if part.active_rm else None

        try:
            self._sim_process.reconfigure(
                partition_name=partition,
                new_rm_name=new_rm,
                new_rm_binary=self._rm_binary_map[new_rm],
                timeout=timeout,
            )

            part.active_rm = rm
            # Clear cached SHM interface since state is fresh
            self._shm_interfaces.pop(f'_shm_{partition}', None)

            logger.info(
                f"Partition '{partition}': reconfigured "
                f"'{old_name}' -> '{new_rm}'"
            )
            return True

        except Exception as e:
            raise PRReconfigurationError(
                f"Failed to reconfigure partition '{partition}' "
                f"to '{new_rm}': {e}"
            ) from e

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

    def get_rm_api(self, partition_name: str, shm=None):
        """
        Get the auto-generated Python API for the currently active RM in a partition.

        This is a convenience method that:
        1. Gets the currently active RM in the partition
        2. Creates a shared memory interface to the partition if not provided
        3. Returns the RM's auto-generated API

        Parameters
        ----------
        partition_name : str
            Name of the partition
        shm : SharedMemoryInterface, optional
            Shared memory interface. If not provided, creates one via shm_for_partition()

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
        api.write_operand_a(42)
        result = api.read_result()
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
        if shm is None:
            shm = self.shm_for_partition(partition_name)

        return active_rm.get_api(shm)

    @property
    def intfs(self) -> Dict:
        """Get all shared memory interface objects for Python interaction."""
        return dict(self._shm_interfaces)

    def _get_partition_shm(self, partition_name: str, target: int = None) -> SharedMemoryInterface:
        """Get or create a SharedMemoryInterface for a partition."""
        cache_key = f'_shm_{partition_name}'
        if cache_key in self._shm_interfaces:
            return self._shm_interfaces[cache_key]

        if self._sim_process is None:
            raise PRReconfigurationError("Simulation is not running")

        if target is None:
            partition = self.partitions[partition_name]
            target = getattr(partition, '_partition_index', 0) + 1

        shm = self._sim_process.get_interface(target=target)
        self._shm_interfaces[cache_key] = shm
        return shm

    def shm_for_partition(self, partition_name: str) -> SharedMemoryInterface:
        """
        Get shared memory interface for a partition.

        Parameters
        ----------
        partition_name : str
            Name of partition to get interface for

        Returns
        -------
        SharedMemoryInterface
            Interface that talks to the partition via shared memory
        """
        if partition_name not in self.partitions:
            raise PRReconfigurationError(f"Unknown partition: '{partition_name}'")

        partition = self.partitions[partition_name]
        target = getattr(partition, '_partition_index', 0) + 1
        return self._get_partition_shm(partition_name, target=target)

    def shm_for_static(self) -> SharedMemoryInterface:
        """
        Get shared memory interface for the static region.

        Returns
        -------
        SharedMemoryInterface
            Interface that talks to the static region via shared memory

        Raises
        ------
        PRReconfigurationError
            If no static region exists or simulation is not running
        """
        if self._static_region is None:
            raise PRReconfigurationError(
                "No static region configured - cannot get interface"
            )

        if not self._running:
            raise PRReconfigurationError(
                "Simulation is not running - call simulate() first"
            )

        cache_key = '_shm_static'
        if cache_key in self._shm_interfaces:
            return self._shm_interfaces[cache_key]

        from .shm_interface import TARGET_STATIC
        shm = self._sim_process.get_interface(target=TARGET_STATIC)
        self._shm_interfaces[cache_key] = shm
        return shm

    def get_static_api(self, shm=None):
        """
        Get the auto-generated Python API for the static region.

        This is a convenience method that:
        1. Gets or creates a shared memory interface to the static region
        2. Returns the static region's auto-generated API

        Parameters
        ----------
        shm : SharedMemoryInterface, optional
            Shared memory interface. If not provided, creates one via shm_for_static()

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
        if shm is None:
            shm = self.shm_for_static()

        return self._static_region.get_api(shm)

    @property
    def ctrl_shm(self) -> SharedMemoryInterface:
        """
        Get shared memory interface for static region control.

        Returns
        -------
        SharedMemoryInterface
            Interface for control (same as shm_for_static)
        """
        return self.shm_for_static()

    def set_isolation(self, partition_name: str, isolated: bool):
        """
        Set isolation state for a partition.

        In DPI mode, isolation is managed implicitly by the single-process
        architecture. During reconfiguration, the RM thread stays alive
        (just swaps the model), so there's no need for explicit isolation
        control. This method is kept for API compatibility.

        Parameters
        ----------
        partition_name : str
            Partition to isolate/release
        isolated : bool
            True to isolate, False to release
        """
        logger.debug(
            f"Isolation {'set' if isolated else 'released'} for {partition_name} "
            f"(handled implicitly in DPI mode)"
        )

    def terminate(self, timeout: float = 10.0):
        """Terminate the simulation."""
        # Close cached shared memory interfaces
        for key, shm in list(self._shm_interfaces.items()):
            try:
                shm.close()
            except Exception:
                pass
        self._shm_interfaces.clear()

        # Terminate simulation process
        if self._sim_process is not None:
            self._sim_process.terminate(timeout=timeout)
            self._sim_process = None

        # Close barrier if present
        if self._barrier is not None:
            self._barrier.close()
            self._barrier = None
            self._barrier_uri = None

        self._running = False
        logger.info("Simulation terminated")

    def wait(self, timeout: float = None):
        """Wait for simulation to complete."""
        if self._sim_process is not None:
            self._sim_process.wait(timeout=timeout)

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
