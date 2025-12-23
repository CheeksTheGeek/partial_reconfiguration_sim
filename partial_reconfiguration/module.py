from typing import Dict, List, Optional, Any, TYPE_CHECKING, Union
from pathlib import Path
import subprocess
import logging

from .exceptions import PRBuildError, PRReconfigurationError

if TYPE_CHECKING:
    from .system import PRSystem
    from .partition import Partition
    from .wrapper_generator import WrapperOutput

logger = logging.getLogger(__name__)


class ReconfigurableModule:
    """
    Represents a reconfigurable module (RM) that can be loaded into a partition.

    Each RM wraps an SbDut and manages:
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
            If True, parse RTL and auto-generate UMI wrapper.
            The wrapper maps each port to a UMI register address,
            enabling Python API access via `get_api()`.
        auto_wrap_config : dict
            Configuration for wrapper generation. Supported keys:
            - enable_read_back: bool (default True) - Make inputs readable
            - enable_interrupts: bool (default True) - Generate interrupt logic
            - address_width: int (default 16) - Address space bits
            - register_width: int (default 32) - Register width
            - clock_name: str (default 'clk') - Clock signal name
            - reset_name: str (default 'rst_n') - Reset signal name
            - reset_active_low: bool (default True) - Reset polarity
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
        self._built = False
        self._queue_uris: Dict[str, str] = {}
        self._intf_defs: Dict[str, Dict] = {}

        self._wrapper_output: Optional['WrapperOutput'] = None
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
        """Create SbDut for this RM."""
        if self._dut is not None:
            return self._dut

        from switchboard import SbDut
        from siliconcompiler import Design

        partition = self.partition
        if partition is None:
            raise PRBuildError(
                f"Cannot build RM '{self.name}': partition '{self.partition_name}' not found"
            )

        intf_defs = {}
        for part_port, part_def in partition.interface.items():
            rm_port = self.port_mapping.get(part_port, part_port)
            intf_def = dict(part_def)
            if part_port in self.port_compatibility:
                intf_def.update(self.port_compatibility[part_port])
            if rm_port in self.interfaces:
                intf_def.update(self.interfaces[rm_port])
            if 'wire' not in intf_def:
                intf_def['wire'] = rm_port

            intf_defs[rm_port] = intf_def

        self._intf_defs = intf_defs
        design_obj = None
        if self.sources:
            top_module_name = self.design or self.name
            design_obj = Design(top_module_name)
            with design_obj.active_fileset('rtl'):
                design_obj.set_topmodule(top_module_name)
                for source in self.sources:
                    source_path = Path(source)
                    if source_path.exists():
                        design_obj.add_file(str(source_path))
                    else:
                        if self.system and self.system.config and self.system.config._source_path:
                            config_dir = self.system.config._source_path.parent
                            rel_path = config_dir / source
                            if rel_path.exists():
                                design_obj.add_file(str(rel_path))
                            else:
                                design_obj.add_file(source)
                        else:
                            design_obj.add_file(source)

            with design_obj.active_fileset('verilator'):
                design_obj.set_topmodule(top_module_name)
                design_obj.add_depfileset(design_obj, 'rtl')
            with design_obj.active_fileset('icarus'):
                design_obj.set_topmodule(top_module_name)
                design_obj.add_depfileset(design_obj, 'rtl')
        elif self.design:
            design_obj = self.design
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
        cycle_sync = False
        if self.system and hasattr(self.system, 'cycle_accurate'):
            cycle_sync = self.system.cycle_accurate

        self._dut = SbDut(
            design=design_obj,
            tool=tool,
            trace=trace,
            trace_type=trace_type,
            frequency=frequency,
            max_rate=max_rate,
            autowrap=True,
            parameters=self.parameters,
            interfaces=intf_defs,
            clocks=self.clocks,
            resets=self.resets,
            tieoffs=self.tieoffs,
            builddir=self._get_build_dir(),
            cycle_sync=cycle_sync
        )

        return self._dut

    def build(self, fast: bool = False) -> 'ReconfigurableModule':
        """
        Build the simulation binary for this RM.

        If the partition has a boundary definition, generates a queue wrapper
        that connects the RM to the static region via queues.

        If auto_wrap is enabled (and no boundary), this will:
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
        ReconfigurableModule
            Self for method chaining
        """
        try:
            if self._has_partition_boundary():
                self._generate_queue_wrapper()
            elif self.auto_wrap and not self._wrapper_output:
                self._generate_auto_wrap()

            dut = self._create_dut()
            dut.build(fast=fast)
            self._built = True
        except Exception as e:
            raise PRBuildError(f"Failed to build RM '{self.name}': {e}") from e

        return self

    def _has_partition_boundary(self) -> bool:
        """Check if the partition has a boundary definition."""
        if self.system is None:
            return False

        partition = self.system.partitions.get(self.partition_name)
        if partition is None:
            return False

        return hasattr(partition, '_boundary_config') and partition._boundary_config is not None

    def _generate_queue_wrapper(self):
        """
        Generate queue wrapper for partition boundary.

        The queue wrapper:
        - Receives inputs from partition queues (from static region)
        - Drives them to the RM
        - Reads RM outputs and sends them to partition queues
        - Also provides UMI interface for Python access
        """
        from .queue_bridge import QueueBridgeGenerator
        from .wrapper_generator import (
            WrapperOutput, AddressMap, AddressRegion, RegisterInfo,
            AccessType, RegisterType
        )
        import json

        partition = self.system.partitions.get(self.partition_name)
        boundary = partition._boundary_config

        build_dir = Path(self._get_build_dir())
        queue_dir = Path(self.system.build_dir).resolve() / 'queues'
        bridge_gen = QueueBridgeGenerator(
            build_dir=str(build_dir),
            queue_dir=str(queue_dir)
        )

        wrapper_path = bridge_gen.generate_rm_side_wrapper(
            boundary=boundary,
            rm_sources=self.sources,
            queue_prefix=self.partition_name,
            rm_module_name=self.design,
            include_umi=True
        )

        self._queue_wrapper_path = wrapper_path
        original_design = self.design
        wrapper_name = f"{self.design}_queue_wrapper"
        self.design = wrapper_name

        self.sources = [str(wrapper_path)] + list(self.sources)
        registers = {}
        addr = 0
        to_rm_ports = [p for p in boundary.ports if p.direction == 'to_rm']
        from_rm_ports = [p for p in boundary.ports if p.direction == 'from_rm']

        for port in to_rm_ports:
            registers[port.name] = RegisterInfo(
                name=port.name,
                address=addr,
                width=port.width,
                access=AccessType.READ_WRITE,
                reg_type=RegisterType.DATA_INPUT,
                port_name=port.name,
                description=f"Input port {port.name} ({port.width} bits)"
            )
            addr += 8

        for port in from_rm_ports:
            registers[port.name] = RegisterInfo(
                name=port.name,
                address=addr,
                width=port.width,
                access=AccessType.READ_ONLY,
                reg_type=RegisterType.DATA_OUTPUT,
                port_name=port.name,
                description=f"Output port {port.name} ({port.width} bits)"
            )
            addr += 8

        region = AddressRegion(
            name="ports",
            base_address=0,
            size=addr,
            registers=registers,
            description="Port registers for queue wrapper"
        )

        address_map = AddressMap(
            regions={"ports": region},
            base_address=0,
            address_width=16,
            data_width=32
        )

        python_class_name = ''.join(word.capitalize() for word in original_design.split('_')) + 'API'
        python_module_code = self._generate_queue_wrapper_python_api(
            original_design, python_class_name, registers
        )

        api_path = build_dir / f"{original_design}_api.py"
        api_path.write_text(python_module_code)

        c_header_code = self._generate_queue_wrapper_c_header(
            original_design, registers
        )

        json_map = {
            "module": original_design,
            "wrapper": wrapper_name,
            "registers": {
                name: {
                    "address": f"0x{reg.address:04X}",
                    "width": reg.width,
                    "access": reg.access.name,
                    "type": reg.reg_type.name
                }
                for name, reg in registers.items()
            }
        }
        json_address_map = json.dumps(json_map, indent=2)

        c_header_path = build_dir / f"{original_design}_regs.h"
        c_header_path.write_text(c_header_code)

        json_path = build_dir / f"{original_design}_address_map.json"
        json_path.write_text(json_address_map)

        self._wrapper_output = WrapperOutput(
            wrapper_rtl=str(wrapper_path),
            wrapper_name=wrapper_name,
            address_map=address_map,
            python_module_code=python_module_code,
            python_class_name=python_class_name,
            c_header_code=c_header_code,
            json_address_map=json_address_map,
            inner_module_name=original_design
        )

        logger.info(f"Generated queue wrapper for RM '{self.name}': {wrapper_path}")

    def _generate_queue_wrapper_python_api(
        self, module_name: str, class_name: str, registers: dict
    ) -> str:
        """Generate Python API class for queue wrapper."""
        lines = []
        lines.append('"""Auto-generated API for queue wrapper UMI interface."""')
        lines.append('')
        lines.append('import numpy as np')
        lines.append('')
        lines.append(f'class {class_name}:')
        lines.append(f'    """')
        lines.append(f'    API for {module_name} queue wrapper.')
        lines.append(f'    ')
        lines.append(f'    Provides Pythonic access to hardware registers via UMI protocol.')
        lines.append(f'    """')
        lines.append('')

        lines.append('    # Register addresses')
        for name, reg in registers.items():
            lines.append(f'    ADDR_{name.upper()} = 0x{reg.address:04X}')
        lines.append('')

        lines.append('    def __init__(self, umi):')
        lines.append('        """')
        lines.append('        Initialize API with UMI interface.')
        lines.append('        ')
        lines.append('        Parameters')
        lines.append('        ----------')
        lines.append('        umi : UmiTxRx')
        lines.append('            Switchboard UMI interface for communication')
        lines.append('        """')
        lines.append('        self._umi = umi')
        lines.append('')

        from .wrapper_generator import RegisterType
        for name, reg in registers.items():
            if reg.width <= 8:
                dtype = 'np.uint8'
            elif reg.width <= 16:
                dtype = 'np.uint16'
            elif reg.width <= 32:
                dtype = 'np.uint32'
            else:
                dtype = 'np.uint64'

            if reg.reg_type == RegisterType.DATA_INPUT:
                lines.append(f'    def write_{name}(self, value):')
                lines.append(f'        """')
                lines.append(f'        Write to {name} register.')
                lines.append(f'        ')
                lines.append(f'        Address: 0x{reg.address:04X}')
                lines.append(f'        Width: {reg.width} bits')
                lines.append(f'        Access: {reg.access.name}')
                lines.append(f'        """')
                lines.append(f'        self._umi.write(self.ADDR_{name.upper()}, {dtype}(value))')
                lines.append('')

                lines.append(f'    def read_{name}(self):')
                lines.append(f'        """')
                lines.append(f'        Read current value of {name} register.')
                lines.append(f'        ')
                lines.append(f'        Address: 0x{reg.address:04X}')
                lines.append(f'        Width: {reg.width} bits')
                lines.append(f'        """')
                lines.append(f'        return int(self._umi.read(self.ADDR_{name.upper()}, {dtype}))')
                lines.append('')

            elif reg.reg_type == RegisterType.DATA_OUTPUT:
                lines.append(f'    def read_{name}(self):')
                lines.append(f'        """')
                lines.append(f'        Read from {name} output.')
                lines.append(f'        ')
                lines.append(f'        Address: 0x{reg.address:04X}')
                lines.append(f'        Width: {reg.width} bits')
                lines.append(f'        Access: {reg.access.name}')
                lines.append(f'        """')
                lines.append(f'        return int(self._umi.read(self.ADDR_{name.upper()}, {dtype}))')
                lines.append('')

        return '\n'.join(lines)

    def _generate_queue_wrapper_c_header(self, module_name: str, registers: dict) -> str:
        """Generate C header file for queue wrapper registers."""
        guard = f"{module_name.upper()}_REGS_H"

        lines = []
        lines.append(f'/*')
        lines.append(f' * Auto-generated register definitions for {module_name} queue wrapper')
        lines.append(f' * Generated by Switchboard PR simulation')
        lines.append(f' */')
        lines.append('')
        lines.append(f'#ifndef {guard}')
        lines.append(f'#define {guard}')
        lines.append('')
        lines.append('#include <stdint.h>')
        lines.append('')

        lines.append('/* Register addresses */')
        for name, reg in registers.items():
            lines.append(f'#define {module_name.upper()}_{name.upper()}_ADDR  0x{reg.address:04X}')
        lines.append('')

        lines.append('/* Register widths (bits) */')
        for name, reg in registers.items():
            lines.append(f'#define {module_name.upper()}_{name.upper()}_WIDTH {reg.width}')
        lines.append('')

        lines.append('/* Helper macros */')
        lines.append(f'#define {module_name.upper()}_BASE_ADDR 0x0000')
        lines.append(f'#define {module_name.upper()}_ADDR_SPACE_SIZE 0x{max(r.address + 8 for r in registers.values()):04X}')
        lines.append('')

        lines.append(f'#endif /* {guard} */')

        return '\n'.join(lines)

    def _generate_auto_wrap(self):
        """
        Parse RTL and generate UMI wrapper.

        This parses the RTL sources to extract port definitions,
        then generates:
        - UMI wrapper RTL (wrapper instantiates original module)
        - Python API module
        - C header file
        - JSON address map

        If ports_override is provided, RTL parsing is skipped and the
        explicit port definitions are used directly.
        """
        from .rtl_parser import RTLParser, PortClassification, ModuleInfo, PortInfo
        from .wrapper_generator import UMIWrapperGenerator, WrapperConfig

        module_name = self.design or self.name

        if self.ports_override:
            logger.info(f"Using explicit ports override for: {self.name}")
            self._module_info = self._create_module_info_from_override(module_name)
        else:
            if not self.sources:
                raise PRBuildError(
                    f"RM '{self.name}' has auto_wrap=True but no sources specified "
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

            logger.info(f"Parsing RTL for auto-wrap: {self.name}")
            parser = RTLParser()
            try:
                self._module_info = parser.parse_module(resolved_sources, module_name)
            except Exception as e:
                raise PRBuildError(f"Failed to parse RTL for '{self.name}': {e}") from e

            warnings = parser.validate_ports(self._module_info)
            for warning in warnings:
                logger.warning(f"[{self.name}] {warning}")

        config = WrapperConfig(
            enable_read_back=self.auto_wrap_config.get('enable_read_back', True),
            enable_interrupts=self.auto_wrap_config.get('enable_interrupts', True),
            enable_wide_burst=self.auto_wrap_config.get('enable_wide_burst', True),
            enable_inout=self.auto_wrap_config.get('enable_inout', True),
            address_width=self.auto_wrap_config.get('address_width', 16),
            register_width=self.auto_wrap_config.get('register_width', 32),
        )

        logger.info(f"Generating UMI wrapper for: {self.name}")
        generator = UMIWrapperGenerator(config)
        self._wrapper_output = generator.generate(self._module_info)

        if not self._wrapper_output.is_valid:
            for error in self._wrapper_output.validation_errors:
                logger.error(f"[{self.name}] {error}")
            raise PRBuildError(
                f"Auto-wrap validation failed for '{self.name}': "
                f"{len(self._wrapper_output.validation_errors)} errors"
            )

        for warning in self._wrapper_output.validation_warnings:
            logger.warning(f"[{self.name}] {warning}")
        build_dir = Path(self._get_build_dir())
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

        logger.info(f"Auto-wrap complete for '{self.name}': {len(self._module_info.ports)} ports mapped")

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
        Get the auto-generated Python API for this RM.

        This method returns an API class that provides typed read/write
        methods for all accessible registers, using the original port names.

        Parameters
        ----------
        umi : UmiTxRx, optional
            UMI interface to use. If not provided, will try to get from
            the partition's interfaces.

        Returns
        -------
        object
            Instance of the generated API class, or None if auto_wrap=False

        Example
        -------
        ```python
        rm = system.get_rm('my_module')
        api = rm.get_api(umi)

        api.write_counter(0x12345678)
        led_state = api.read_led_status()
        ```
        """
        if not self.auto_wrap:
            logger.warning(
                f"get_api() called on RM '{self.name}' but auto_wrap=False"
            )
            return None

        if self._wrapper_output is None:
            raise PRBuildError(
                f"RM '{self.name}' has not been built yet. Call build() first."
            )

        if umi is None:
            if self.partition and hasattr(self.partition, '_intfs'):
                for intf_name, intf in self.partition._intfs.items():
                    if hasattr(intf, 'read') and hasattr(intf, 'write'):
                        umi = intf
                        break

            if umi is None:
                raise ValueError(
                    f"No UMI interface provided and could not find one for RM '{self.name}'"
                )
        if self._api_class is None:
            import importlib.util
            import sys

            build_dir = Path(self._get_build_dir())
            module_name_for_api = self._wrapper_output.inner_module_name
            api_path = build_dir / f"{module_name_for_api}_api.py"

            if not api_path.exists():
                raise PRBuildError(f"API file not found: {api_path}")

            module_name = f"generated_api_{self.name}"
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

    def configure_queues(self, queue_uris: Dict[str, str]):
        """
        Configure queue URIs for partition ports.

        This maps partition port names to queue URIs, then translates
        to RM port names using the port mapping.

        Parameters
        ----------
        queue_uris : dict
            Mapping from partition port names to queue URIs
        """
        self._queue_uris = {}

        for part_port, uri in queue_uris.items():
            rm_port = self.port_mapping.get(part_port, part_port)
            self._queue_uris[rm_port] = uri

            if rm_port in self._intf_defs:
                self._intf_defs[rm_port]['uri'] = uri
        if self._dut is not None:
            for rm_port, uri in self._queue_uris.items():
                if rm_port in self._dut.intf_defs:
                    self._dut.intf_defs[rm_port]['uri'] = uri

    def start(self, extra_plusargs: List[str] = None) -> subprocess.Popen:
        """
        Start the RM simulation process.

        This creates a fresh Verilator process with reset state.
        All registers will be initialized to their reset values.

        Note: Python interface objects are NOT created here - they are
        owned by the Partition and persist across RM swaps.

        Parameters
        ----------
        extra_plusargs : list, optional
            Additional plusargs to pass to the simulator.
            Used for barrier synchronization in cycle-accurate mode.

        Returns
        -------
        subprocess.Popen
            Running process handle
        """
        if not self._built:
            self.build()

        dut = self._dut

        for rm_port, uri in self._queue_uris.items():
            if rm_port in dut.intf_defs:
                dut.intf_defs[rm_port]['uri'] = uri
                logger.info(f"[{self.name}] Set interface '{rm_port}' URI to '{uri}'")
            else:
                logger.warning(f"[{self.name}] Interface '{rm_port}' not found in dut.intf_defs: {list(dut.intf_defs.keys())}")

        # Debug: print all interface definitions and plusargs
        logger.info(f"[{self.name}] intf_defs before simulate: {dut.intf_defs}")

        # Debug: print what plusargs will be generated
        plusargs = []
        for name, value in dut.intf_defs.items():
            wire = value.get('wire', None)
            uri = value.get('uri', None)
            if (wire is not None) and (uri is not None):
                plusargs.append((wire, uri))
        logger.info(f"[{self.name}] Expected plusargs: {plusargs}")
        try:
            self._process = dut.simulate(
                plusargs=extra_plusargs or [],
                intf_objs=False
            )
        except Exception as e:
            raise PRReconfigurationError(
                f"Failed to start RM '{self.name}': {e}"
            ) from e

        return self._process

    def terminate(self, timeout: float = 10.0):
        """
        Terminate the RM simulation process and wait for it to exit.

        This method ensures the process has fully exited before returning.
        This is critical for PR simulation - we must guarantee the old RM
        is completely stopped before starting a new one, otherwise both
        processes could be accessing the same queues simultaneously.

        Parameters
        ----------
        timeout : float
            Timeout for graceful termination before force kill
        """
        if self._process is None:
            return

        try:
            if self._dut is not None:
                self._dut.terminate(stop_timeout=timeout)

            if self._process is not None:
                try:
                    self._process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait()

        except Exception:
            if self._process is not None:
                try:
                    self._process.kill()
                    self._process.wait()
                except Exception:
                    pass

        self._process = None

    @property
    def is_running(self) -> bool:
        """Check if RM process is currently running."""
        if self._process is None:
            return False
        return self._process.poll() is None

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
