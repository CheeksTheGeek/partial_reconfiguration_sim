from typing import Dict, List, Optional, Tuple, Any, TYPE_CHECKING
from enum import Enum

from .exceptions import PRValidationError

if TYPE_CHECKING:
    from .partition import Partition
    from .module import ReconfigurableModule


class CompatibilityPolicy(Enum):
    """Port compatibility policy levels."""

    STRICT = "strict"
    """Exact port match required - same ports, same widths, same types."""

    SUPERSET = "superset"
    """
    Partition interface is superset of RM interface.
    RM can have fewer ports (unused partition ports tied off).
    RM ports can be narrower (zero-extended to partition width).
    """

    RELAXED = "relaxed"
    """
    Most permissive mode.
    Width mismatches allowed in both directions.
    Missing ports allowed on either side.
    """


class PortCompatibilityResult:
    """Result of port compatibility validation."""

    def __init__(self):
        self.compatible: bool = True
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.tieoffs: Dict[str, Any] = {}  # Ports that need tie-off
        self.width_adjustments: Dict[str, Tuple[int, int]] = {}  # port -> (rm_width, part_width)

    def add_error(self, message: str):
        """Add an error (makes result incompatible)."""
        self.errors.append(message)
        self.compatible = False

    def add_warning(self, message: str):
        """Add a warning (doesn't affect compatibility)."""
        self.warnings.append(message)

    def add_tieoff(self, port_name: str, value: Any = 0):
        """Mark port as needing tie-off."""
        self.tieoffs[port_name] = value

    def add_width_adjustment(self, port_name: str, rm_width: int, part_width: int):
        """Record width adjustment needed."""
        self.width_adjustments[port_name] = (rm_width, part_width)


class PortValidator:
    """
    Validates port compatibility between partition interface and RMs.

    Supports three policies:
    - strict: Exact match required
    - superset: Partition is superset of RM (like real PR)
    - relaxed: Most permissive, useful for testing
    """
    DEFAULT_WIDTHS = {
        'sb': {'dw': 256},
        'axi': {'dw': 32, 'aw': 32, 'idw': 4},
        'axil': {'dw': 32, 'aw': 32},
        'apb': {'dw': 32, 'aw': 32},
        'gpio': {'width': 1},
    }

    def __init__(self, policy: str = 'superset'):
        """
        Initialize validator.

        Parameters
        ----------
        policy : str
            Compatibility policy ('strict', 'superset', 'relaxed')
        """
        self.policy = CompatibilityPolicy(policy)

    def validate_partition(self, partition: 'Partition') -> bool:
        """
        Validate all RMs registered with a partition.

        Parameters
        ----------
        partition : Partition
            Partition to validate

        Returns
        -------
        bool
            True if all RMs are compatible

        Raises
        ------
        PRValidationError
            If any RM is incompatible (in strict/superset mode)
        """
        all_valid = True

        for rm in partition.registered_rms.values():
            result = self.validate_rm_compatibility(partition, rm)
            if not result.compatible:
                all_valid = False
                raise PRValidationError(
                    f"Port compatibility errors for RM '{rm.name}' "
                    f"in partition '{partition.name}':\n"
                    + "\n".join(f"  - {e}" for e in result.errors)
                )
            for warning in result.warnings:
                print(f"Warning [{rm.name}]: {warning}")

        return all_valid

    def validate_rm_compatibility(
        self,
        partition: 'Partition',
        rm: 'ReconfigurableModule'
    ) -> PortCompatibilityResult:
        """
        Validate RM compatibility with partition interface.

        Parameters
        ----------
        partition : Partition
            Target partition
        rm : ReconfigurableModule
            RM to validate

        Returns
        -------
        PortCompatibilityResult
            Detailed validation result
        """
        result = PortCompatibilityResult()

        part_interface = partition.interface
        port_mapping = rm.port_mapping
        port_compat = rm.port_compatibility
        mapped_rm_ports = set(port_mapping.values())
        for part_port, part_def in part_interface.items():
            rm_port = port_mapping.get(part_port, part_port)
            is_mapped = (part_port in port_mapping or
                         part_port in mapped_rm_ports or
                         rm_port in mapped_rm_ports)

            if not is_mapped and part_port not in port_mapping:
                if self.policy == CompatibilityPolicy.STRICT:
                    result.add_error(
                        f"Missing port mapping for partition port '{part_port}'"
                    )
                else:
                    result.add_warning(
                        f"Port '{part_port}' not mapped, will be tied off"
                    )
                    result.add_tieoff(part_port, 0)
                continue
            rm_def = port_compat.get(part_port, {})
            rm_type = rm_def.get('type', part_def['type'])
            if rm_type != part_def['type']:
                result.add_error(
                    f"Port type mismatch for '{part_port}': "
                    f"partition={part_def['type']}, RM={rm_type}"
                )
            rm_dir = rm_def.get('direction', part_def['direction'])
            if not self._directions_compatible(rm_dir, part_def['direction']):
                result.add_error(
                    f"Port direction mismatch for '{part_port}': "
                    f"partition={part_def['direction']}, RM={rm_dir}"
                )
            part_width = self._get_port_width(part_def)
            rm_width = self._get_port_width(rm_def) if rm_def else part_width

            if rm_width != part_width:
                self._check_width_compatibility(
                    part_port, rm_width, part_width, part_def, result
                )
        if self.policy == CompatibilityPolicy.STRICT:
            for rm_port in mapped_rm_ports:
                found = False
                for part_port, mapping in port_mapping.items():
                    if mapping == rm_port:
                        found = True
                        break
                if not found and rm_port not in part_interface:
                    result.add_error(
                        f"RM has port '{rm_port}' not in partition interface"
                    )

        return result

    def validate_rm_config(
        self,
        partition_interface: Dict[str, Dict],
        rm_port_mapping: Dict[str, str],
        rm_port_compatibility: Dict[str, Dict] = None
    ) -> PortCompatibilityResult:
        """
        Validate RM configuration against partition interface (dict-based).

        Parameters
        ----------
        partition_interface : dict
            Partition interface definition
        rm_port_mapping : dict
            RM port mapping (partition_port -> rm_port)
        rm_port_compatibility : dict, optional
            Port compatibility overrides

        Returns
        -------
        PortCompatibilityResult
            Validation result
        """
        result = PortCompatibilityResult()
        port_compat = rm_port_compatibility or {}

        for part_port, part_def in partition_interface.items():
            rm_port = rm_port_mapping.get(part_port, part_port)
            if part_port not in rm_port_mapping:
                if self.policy == CompatibilityPolicy.STRICT:
                    result.add_error(
                        f"Missing port mapping for '{part_port}'"
                    )
                else:
                    result.add_tieoff(part_port, 0)
                continue
            rm_def = port_compat.get(part_port, {})
            rm_type = rm_def.get('type', part_def['type'])
            if rm_type != part_def['type']:
                result.add_error(
                    f"Type mismatch for '{part_port}': "
                    f"expected {part_def['type']}, got {rm_type}"
                )
            rm_dir = rm_def.get('direction', part_def['direction'])
            if not self._directions_compatible(rm_dir, part_def['direction']):
                result.add_error(
                    f"Direction mismatch for '{part_port}'"
                )
            part_width = self._get_port_width(part_def)
            rm_width = self._get_port_width(rm_def) if rm_def else part_width

            if rm_width != part_width:
                self._check_width_compatibility(
                    part_port, rm_width, part_width, part_def, result
                )

        return result

    def _directions_compatible(self, rm_dir: str, part_dir: str) -> bool:
        """Check if directions are compatible."""
        if rm_dir == part_dir:
            return True
        if rm_dir in ['manager', 'output'] and part_dir in ['manager', 'output']:
            return True
        if rm_dir in ['subordinate', 'input'] and part_dir in ['subordinate', 'input']:
            return True

        return False

    def _get_port_width(self, port_def: Dict) -> int:
        """Extract primary width from port definition."""
        port_type = port_def.get('type', 'gpio')
        if 'width' in port_def:
            return port_def['width']
        if 'dw' in port_def:
            return port_def['dw']
        defaults = self.DEFAULT_WIDTHS.get(port_type, {})
        if 'dw' in defaults:
            return defaults['dw']
        if 'width' in defaults:
            return defaults['width']

        return 1  # Default for GPIO-like signals

    def _check_width_compatibility(
        self,
        port_name: str,
        rm_width: int,
        part_width: int,
        part_def: Dict,
        result: PortCompatibilityResult
    ):
        """Check width compatibility and record adjustments."""
        if self.policy == CompatibilityPolicy.STRICT:
            result.add_error(
                f"Width mismatch for '{port_name}': "
                f"partition={part_width}, RM={rm_width}"
            )
        elif self.policy == CompatibilityPolicy.SUPERSET:
            if rm_width > part_width:
                result.add_error(
                    f"RM port '{port_name}' is wider ({rm_width}) "
                    f"than partition ({part_width})"
                )
            else:
                result.add_warning(
                    f"Port '{port_name}' width mismatch: "
                    f"RM={rm_width}, partition={part_width}. "
                    "Will be zero-extended."
                )
                result.add_width_adjustment(port_name, rm_width, part_width)
        else:  # RELAXED
            result.add_warning(
                f"Port '{port_name}' width mismatch: "
                f"RM={rm_width}, partition={part_width}"
            )
            result.add_width_adjustment(port_name, rm_width, part_width)

    def generate_tieoff_config(
        self,
        partition_interface: Dict[str, Dict],
        rm_port_mapping: Dict[str, str],
        default_value: int = 0
    ) -> Dict[str, Any]:
        """
        Generate tie-off configuration for unmapped ports.

        Parameters
        ----------
        partition_interface : dict
            Partition interface definition
        rm_port_mapping : dict
            RM port mapping
        default_value : int
            Default tie-off value

        Returns
        -------
        dict
            Tie-off configuration (port_name -> value)
        """
        tieoffs = {}

        for part_port, part_def in partition_interface.items():
            if part_port not in rm_port_mapping:
                direction = part_def.get('direction', 'input')
                if direction in ['output', 'manager']:
                    tieoffs[part_port] = default_value

        return tieoffs

    def validate_auto_wrap_port_consistency(
        self,
        partition: 'Partition',
        strict: bool = True
    ) -> PortCompatibilityResult:
        """
        Validate that all auto-wrapped RMs in a partition have consistent ports.

        In FPGA partial reconfiguration, all RMs for a partition must have the
        same interface. This method validates that when auto_wrap is enabled,
        all RMs in the same partition expose the same ports.

        Parameters
        ----------
        partition : Partition
            Partition to validate
        strict : bool
            If True (default), all ports must match exactly.
            If False, only common ports must be compatible.

        Returns
        -------
        PortCompatibilityResult
            Validation result with errors/warnings

        Example
        -------
        ```yaml
        reconfigurable_modules:
          - name: blinking_led
            partition: pr_led
            auto_wrap: true
            sources: [rtl/blinking_led.sv]
          - name: blinking_led_slow
            partition: pr_led
            auto_wrap: true
            sources: [rtl/blinking_led_slow.sv]
        ```
        """
        result = PortCompatibilityResult()
        auto_wrap_rms = [
            rm for rm in partition.registered_rms.values()
            if rm.auto_wrap
        ]

        if len(auto_wrap_rms) < 2:
            return result
        reference_rm = auto_wrap_rms[0]
        if reference_rm._module_info is None:
            if reference_rm.ports_override:
                reference_rm._module_info = reference_rm._create_module_info_from_override(
                    reference_rm.design or reference_rm.name
                )
            else:
                result.add_warning(
                    f"Cannot validate port consistency: RM '{reference_rm.name}' "
                    f"has not been parsed yet. Run validation after build."
                )
                return result

        reference_ports = {p.name: p for p in reference_rm._module_info.ports}
        for rm in auto_wrap_rms[1:]:
            if rm._module_info is None:
                if rm.ports_override:
                    rm._module_info = rm._create_module_info_from_override(
                        rm.design or rm.name
                    )
                else:
                    result.add_warning(
                        f"Cannot validate RM '{rm.name}': not parsed yet"
                    )
                    continue

            rm_ports = {p.name: p for p in rm._module_info.ports}
            for port_name, ref_port in reference_ports.items():
                if port_name not in rm_ports:
                    if strict:
                        result.add_error(
                            f"RM '{rm.name}' missing port '{port_name}' "
                            f"(present in '{reference_rm.name}')"
                        )
                    else:
                        result.add_warning(
                            f"RM '{rm.name}' missing optional port '{port_name}'"
                        )
                    continue

                rm_port = rm_ports[port_name]
                if rm_port.direction != ref_port.direction:
                    result.add_error(
                        f"Port '{port_name}' direction mismatch: "
                        f"'{reference_rm.name}'={ref_port.direction}, "
                        f"'{rm.name}'={rm_port.direction}"
                    )
                if rm_port.width != ref_port.width:
                    if strict:
                        result.add_error(
                            f"Port '{port_name}' width mismatch: "
                            f"'{reference_rm.name}'={ref_port.width}, "
                            f"'{rm.name}'={rm_port.width}"
                        )
                    else:
                        result.add_warning(
                            f"Port '{port_name}' width differs: "
                            f"'{reference_rm.name}'={ref_port.width}, "
                            f"'{rm.name}'={rm_port.width}"
                        )
                if rm_port.port_type != ref_port.port_type:
                    result.add_warning(
                        f"Port '{port_name}' type differs: "
                        f"'{reference_rm.name}'={ref_port.port_type}, "
                        f"'{rm.name}'={rm_port.port_type}"
                    )
            if strict:
                for port_name in rm_ports:
                    if port_name not in reference_ports:
                        result.add_error(
                            f"RM '{rm.name}' has extra port '{port_name}' "
                            f"(not in '{reference_rm.name}')"
                        )

        return result
