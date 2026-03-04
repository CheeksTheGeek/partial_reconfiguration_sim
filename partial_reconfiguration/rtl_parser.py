import logging
import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional, Tuple, Set, Any
from pathlib import Path

import pyslang
import pyslang.ast
import pyslang.syntax

logger = logging.getLogger(__name__)


class PortType(Enum):
    """Port type classification."""
    CLOCK = auto()
    RESET = auto()
    DATA = auto()
    ENABLE = auto()
    INTERRUPT = auto()


class ResetPolarity(Enum):
    """Reset signal polarity."""
    ACTIVE_HIGH = auto()
    ACTIVE_LOW = auto()
    UNKNOWN = auto()


@dataclass
class PortInfo:
    """Information about a module port."""
    name: str
    direction: str  # 'input', 'output', 'inout'
    width: int
    port_type: PortType
    is_signed: bool = False
    is_array: bool = False
    array_dims: Tuple[int, ...] = ()
    reset_polarity: ResetPolarity = ResetPolarity.UNKNOWN
    clock_domain: Optional[str] = None  # For multi-clock designs
    description: str = ""

    @property
    def total_bits(self) -> int:
        """Total bits including array dimensions."""
        total = self.width
        for dim in self.array_dims:
            total *= dim
        return total

    @property
    def is_wide(self) -> bool:
        """Check if port is wider than 64 bits."""
        return self.total_bits > 64

    def __repr__(self):
        width_str = f"[{self.width-1}:0]" if self.width > 1 else ""
        array_str = "".join(f"[{d}]" for d in self.array_dims) if self.array_dims else ""
        return f"PortInfo({self.direction} {width_str}{self.name}{array_str}, type={self.port_type.name})"


@dataclass
class ParameterInfo:
    """Information about a module parameter."""
    name: str
    value: Any
    width: int = 32
    is_localparam: bool = False
    description: str = ""


@dataclass
class ModuleInfo:
    """Information about a parsed module."""
    name: str
    ports: Dict[str, PortInfo] = field(default_factory=dict)
    parameters: Dict[str, ParameterInfo] = field(default_factory=dict)
    source_file: Optional[str] = None

    def input_ports(self) -> List[PortInfo]:
        """Return list of input ports."""
        return [p for p in self.ports.values() if p.direction == 'input']

    def output_ports(self) -> List[PortInfo]:
        """Return list of output ports."""
        return [p for p in self.ports.values() if p.direction == 'output']

    def inout_ports(self) -> List[PortInfo]:
        """Return list of bidirectional ports."""
        return [p for p in self.ports.values() if p.direction == 'inout']

    def data_ports(self) -> List[PortInfo]:
        """Return list of data ports (excluding clock/reset/enable)."""
        return [p for p in self.ports.values() if p.port_type == PortType.DATA]

    def clock_ports(self) -> List[PortInfo]:
        """Return list of clock ports."""
        return [p for p in self.ports.values() if p.port_type == PortType.CLOCK]

    def reset_ports(self) -> List[PortInfo]:
        """Return list of reset ports."""
        return [p for p in self.ports.values() if p.port_type == PortType.RESET]

    def interrupt_ports(self) -> List[PortInfo]:
        """Return list of interrupt ports."""
        return [p for p in self.ports.values() if p.port_type == PortType.INTERRUPT]

    def wide_ports(self) -> List[PortInfo]:
        """Return list of ports wider than 64 bits."""
        return [p for p in self.ports.values() if p.is_wide]

    def get_primary_clock(self) -> Optional[PortInfo]:
        """Get the primary clock (first clock found)."""
        clocks = self.clock_ports()
        return clocks[0] if clocks else None

    def get_primary_reset(self) -> Optional[PortInfo]:
        """Get the primary reset (first reset found)."""
        resets = self.reset_ports()
        return resets[0] if resets else None


@dataclass
class PortClassification:
    """
    Configuration for port classification.

    Allows explicit specification of clock/reset/data ports,
    overriding automatic detection.
    """
    clocks: Set[str] = field(default_factory=set)
    resets: Dict[str, ResetPolarity] = field(default_factory=dict)
    enables: Set[str] = field(default_factory=set)
    interrupts: Set[str] = field(default_factory=set)
    data: Set[str] = field(default_factory=set)
    clock_patterns: Tuple[str, ...] = (
        r'^clk$', r'^clock$', r'^ck$',
        r'^clk_\w+$', r'^\w+_clk$',
        r'^clock_\w+$', r'^\w+_clock$',
        r'^pclk$', r'^hclk$', r'^fclk$', r'^aclk$',
        r'^sysclk$', r'^refclk$', r'^coreclk$',
    )

    reset_patterns: Tuple[Tuple[str, ResetPolarity], ...] = (
        (r'^rst_n$', ResetPolarity.ACTIVE_LOW),
        (r'^rstn$', ResetPolarity.ACTIVE_LOW),
        (r'^reset_n$', ResetPolarity.ACTIVE_LOW),
        (r'^resetn$', ResetPolarity.ACTIVE_LOW),
        (r'^nreset$', ResetPolarity.ACTIVE_LOW),
        (r'^arst_n$', ResetPolarity.ACTIVE_LOW),
        (r'^\w+_rst_n$', ResetPolarity.ACTIVE_LOW),
        (r'^\w+_resetn$', ResetPolarity.ACTIVE_LOW),
        (r'^rst$', ResetPolarity.ACTIVE_HIGH),
        (r'^reset$', ResetPolarity.ACTIVE_HIGH),
        (r'^arst$', ResetPolarity.ACTIVE_HIGH),
        (r'^srst$', ResetPolarity.ACTIVE_HIGH),
        (r'^\w+_rst$', ResetPolarity.ACTIVE_HIGH),
        (r'^\w+_reset$', ResetPolarity.ACTIVE_HIGH),
    )

    enable_patterns: Tuple[str, ...] = (
        r'^en$', r'^enable$',
        r'^\w+_en$', r'^\w+_enable$',
        r'^clk_en$', r'^clken$',
    )

    interrupt_patterns: Tuple[str, ...] = (
        r'^irq$', r'^interrupt$', r'^int$',
        r'^\w+_irq$', r'^\w+_int$',
        r'^irq_\w+$', r'^int_\w+$',
    )
    exclude_from_clock: Tuple[str, ...] = (
        r'.*_en$', r'.*_enable$', r'.*_gate$',
        r'.*_div$', r'.*_sel$', r'.*_mux$',
    )

    exclude_from_reset: Tuple[str, ...] = (
        r'.*_en$', r'.*_enable$',
        r'.*_count.*', r'.*_cnt.*',
        r'.*_val.*', r'.*_data.*',
    )


class RTLParser:
    """
    Parse RTL files to extract module port definitions using pyslang.

    Features:
    - Configurable port classification with exact match and patterns
    - Parameter extraction
    - Include directory support
    - Multi-file parsing
    - Support for wide and array ports

    Example usage:
        parser = RTLParser()
        module_info = parser.parse_module(['blinking_led.sv'], 'blinking_led')
        classification = PortClassification(
            clocks={'phi1', 'phi2'},  # Explicit clock names
            resets={'clear': ResetPolarity.ACTIVE_HIGH}
        )
        parser = RTLParser(classification=classification)
        module_info = parser.parse_module(['design.sv'], 'my_design')
    """

    def __init__(
        self,
        classification: PortClassification = None,
        strict_matching: bool = False
    ):
        """
        Initialize RTL parser.

        Parameters
        ----------
        classification : PortClassification, optional
            Custom port classification rules
        strict_matching : bool
            If True, only use explicit classifications, not patterns
        """
        self.classification = classification or PortClassification()
        self.strict_matching = strict_matching
        self._compiled_patterns: Dict[str, List[re.Pattern]] = {}
        self._compile_patterns()

    def _compile_patterns(self):
        """Pre-compile regex patterns for efficiency."""
        self._compiled_patterns['clock'] = [
            re.compile(p, re.IGNORECASE) for p in self.classification.clock_patterns
        ]
        self._compiled_patterns['reset'] = [
            (re.compile(p, re.IGNORECASE), pol)
            for p, pol in self.classification.reset_patterns
        ]
        self._compiled_patterns['enable'] = [
            re.compile(p, re.IGNORECASE) for p in self.classification.enable_patterns
        ]
        self._compiled_patterns['interrupt'] = [
            re.compile(p, re.IGNORECASE) for p in self.classification.interrupt_patterns
        ]
        self._compiled_patterns['exclude_clock'] = [
            re.compile(p, re.IGNORECASE) for p in self.classification.exclude_from_clock
        ]
        self._compiled_patterns['exclude_reset'] = [
            re.compile(p, re.IGNORECASE) for p in self.classification.exclude_from_reset
        ]

    def parse_module(
        self,
        sources: List[str],
        module_name: str = None,
        include_dirs: List[str] = None,
        defines: Dict[str, str] = None
    ) -> ModuleInfo:
        """
        Parse RTL files and extract module port information.

        Parameters
        ----------
        sources : list of str
            List of source file paths to parse
        module_name : str, optional
            Name of module to extract. If None, uses the first top-level module.
        include_dirs : list of str, optional
            Additional include directories for preprocessing
        defines : dict, optional
            Preprocessor defines (name -> value)

        Returns
        -------
        ModuleInfo
            Information about the parsed module including all ports
        """
        source_paths = [str(Path(s).resolve()) for s in sources]
        options = pyslang.Bag()
        if include_dirs:
            include_paths = [str(Path(d).resolve()) for d in include_dirs]
            for inc_path in include_paths:
                options.set('includeSystemPaths', inc_path)
        trees = []
        for path in source_paths:
            try:
                tree = pyslang.syntax.SyntaxTree.fromFile(path)
                if tree is None:
                    raise ValueError(f"Failed to parse file: {path}")
                trees.append(tree)
            except Exception as e:
                raise ValueError(f"Error parsing {path}: {e}")
        compilation = pyslang.ast.Compilation()
        for tree in trees:
            compilation.addSyntaxTree(tree)
        diags = compilation.getAllDiagnostics()
        errors = []
        warnings = []
        for d in diags:
            try:
                if hasattr(d, 'severity'):
                    if d.severity == pyslang.DiagnosticSeverity.Error:
                        errors.append(d)
                    elif d.severity == pyslang.DiagnosticSeverity.Warning:
                        warnings.append(d)
                else:
                    d_str = str(d).lower()
                    if 'error' in d_str:
                        errors.append(d)
                    elif 'warning' in d_str:
                        warnings.append(d)
            except Exception:
                warnings.append(d)

        if errors:
            error_msgs = [str(e) for e in errors[:5]]  # Limit to first 5
            logger.error(f"Parse errors: {error_msgs}")

        if warnings:
            logger.debug(f"Parse warnings: {len(warnings)} warnings")
        root = compilation.getRoot()
        top_instances = list(root.topInstances)

        if not top_instances:
            raise ValueError(f"No top-level modules found in sources: {sources}")
        target_instance = None
        if module_name:
            for inst in top_instances:
                if inst.name == module_name:
                    target_instance = inst
                    break
            if target_instance is None:
                available = [inst.name for inst in top_instances]
                raise ValueError(
                    f"Module '{module_name}' not found. Available: {available}"
                )
        else:
            target_instance = top_instances[0]
            module_name = target_instance.name
            logger.info(f"Auto-selected module: {module_name}")
        ports = self._extract_ports(target_instance)
        parameters = self._extract_parameters(target_instance)

        return ModuleInfo(
            name=module_name,
            ports=ports,
            parameters=parameters,
            source_file=source_paths[0] if source_paths else None
        )

    def _extract_ports(self, instance) -> Dict[str, PortInfo]:
        """Extract port information from a module instance."""
        ports = {}
        body = instance.body

        for port in body.portList:
            port_info = self._parse_port(port)
            if port_info:
                ports[port_info.name] = port_info

        return ports

    def _extract_parameters(self, instance) -> Dict[str, ParameterInfo]:
        """Extract parameter information from a module instance."""
        parameters = {}
        try:
            body = instance.body
            if hasattr(body, 'parameters'):
                for param in body.parameters:
                    try:
                        param_info = self._parse_parameter(param)
                        if param_info:
                            parameters[param_info.name] = param_info
                    except Exception as e:
                        logger.debug(f"Failed to parse parameter: {e}")
        except Exception as e:
            logger.debug(f"Could not extract parameters: {e}")

        return parameters

    def _parse_parameter(self, param) -> Optional[ParameterInfo]:
        """Parse a parameter symbol into ParameterInfo."""
        try:
            name = param.name
            value = None
            if hasattr(param, 'value'):
                try:
                    value = param.value
                except:
                    pass
            is_local = 'localparam' in str(type(param)).lower()

            return ParameterInfo(
                name=name,
                value=value,
                is_localparam=is_local
            )
        except Exception as e:
            logger.debug(f"Failed to parse parameter: {e}")
            return None

    def _parse_port(self, port) -> Optional[PortInfo]:
        """Parse a single port symbol into PortInfo."""
        try:
            name = port.name
            direction = self._get_direction(port.direction)
            if direction is None:
                logger.warning(f"Skipping port with unknown direction: {name}")
                return None
            width = 1
            is_signed = False
            is_array = False
            array_dims = ()

            if hasattr(port, 'type'):
                port_type = port.type
                if hasattr(port_type, 'bitWidth'):
                    width = port_type.bitWidth
                if hasattr(port_type, 'isSigned'):
                    is_signed = port_type.isSigned
                if hasattr(port_type, 'getFixedRange'):
                    try:
                        range_info = port_type.getFixedRange()
                        if range_info:
                            pass
                    except:
                        pass
            port_type_enum, reset_polarity = self._classify_port(name, direction, width)

            return PortInfo(
                name=name,
                direction=direction,
                width=width,
                port_type=port_type_enum,
                is_signed=is_signed,
                is_array=is_array,
                array_dims=array_dims,
                reset_polarity=reset_polarity
            )
        except Exception as e:
            logger.warning(f"Failed to parse port {port}: {e}")
            return None

    def _get_direction(self, arg_direction) -> Optional[str]:
        """Convert pyslang ArgumentDirection to string."""
        direction_map = {
            pyslang.ast.ArgumentDirection.In: 'input',
            pyslang.ast.ArgumentDirection.Out: 'output',
            pyslang.ast.ArgumentDirection.InOut: 'inout',
        }
        return direction_map.get(arg_direction)

    def _classify_port(
        self,
        name: str,
        direction: str,
        width: int
    ) -> Tuple[PortType, ResetPolarity]:
        """
        Classify a port as clock, reset, enable, interrupt, or data.

        Classification priority:
        1. Explicit classification (highest)
        2. Pattern matching (if not strict_matching)
        3. Default to DATA

        Returns
        -------
        Tuple[PortType, ResetPolarity]
            The port type and reset polarity (if applicable)
        """
        reset_polarity = ResetPolarity.UNKNOWN
        if name in self.classification.clocks:
            return PortType.CLOCK, reset_polarity

        if name in self.classification.resets:
            return PortType.RESET, self.classification.resets[name]

        if name in self.classification.enables:
            return PortType.ENABLE, reset_polarity

        if name in self.classification.interrupts:
            return PortType.INTERRUPT, reset_polarity

        if name in self.classification.data:
            return PortType.DATA, reset_polarity
        if self.strict_matching:
            return PortType.DATA, reset_polarity
        if direction == 'input' and width == 1:
            if not self._matches_any(name, self._compiled_patterns['exclude_clock']):
                if self._matches_any(name, self._compiled_patterns['clock']):
                    return PortType.CLOCK, reset_polarity

            if not self._matches_any(name, self._compiled_patterns['exclude_reset']):
                for pattern, polarity in self._compiled_patterns['reset']:
                    if pattern.match(name):
                        return PortType.RESET, polarity
            if self._matches_any(name, self._compiled_patterns['enable']):
                return PortType.ENABLE, reset_polarity
        if direction == 'output':
            if self._matches_any(name, self._compiled_patterns['interrupt']):
                return PortType.INTERRUPT, reset_polarity
        return PortType.DATA, reset_polarity

    def _matches_any(self, name: str, patterns: List[re.Pattern]) -> bool:
        """Check if name matches any of the compiled patterns."""
        return any(p.match(name) for p in patterns)

    def parse_text(self, sv_code: str, module_name: str = None) -> ModuleInfo:
        """
        Parse SystemVerilog code from a string.

        Parameters
        ----------
        sv_code : str
            SystemVerilog source code
        module_name : str, optional
            Name of module to extract

        Returns
        -------
        ModuleInfo
            Information about the parsed module
        """
        tree = pyslang.syntax.SyntaxTree.fromText(sv_code)
        compilation = pyslang.ast.Compilation()
        compilation.addSyntaxTree(tree)

        root = compilation.getRoot()
        top_instances = list(root.topInstances)

        if not top_instances:
            raise ValueError("No modules found in provided code")
        target_instance = None
        if module_name:
            for inst in top_instances:
                if inst.name == module_name:
                    target_instance = inst
                    break
            if target_instance is None:
                raise ValueError(f"Module '{module_name}' not found")
        else:
            target_instance = top_instances[0]
            module_name = target_instance.name

        ports = self._extract_ports(target_instance)
        parameters = self._extract_parameters(target_instance)

        return ModuleInfo(
            name=module_name,
            ports=ports,
            parameters=parameters
        )

    def validate_ports(self, module_info: ModuleInfo) -> List[str]:
        """
        Validate parsed ports for potential issues.

        Returns list of warning messages.
        """
        warnings = []
        if not module_info.clock_ports():
            if any(p.direction == 'output' for p in module_info.ports.values()):
                warnings.append("No clock port detected - module may be purely combinational")

        clocks = module_info.clock_ports()
        if len(clocks) > 1:
            warnings.append(f"Multiple clocks detected: {[c.name for c in clocks]} - consider specifying clock domains")

        for port in module_info.wide_ports():
            warnings.append(f"Wide port '{port.name}' ({port.total_bits} bits) may require burst transfers")

        for port in module_info.inout_ports():
            warnings.append(f"Bidirectional port '{port.name}' requires special handling")

        return warnings
