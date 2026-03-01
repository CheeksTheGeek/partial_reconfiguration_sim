from typing import Dict, List, Optional, Union, Any
from pathlib import Path
import json
import copy

from .exceptions import PRConfigError

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

try:
    import tomllib
    HAS_TOML = True
except ImportError:
    try:
        import tomli as tomllib
        HAS_TOML = True
    except ImportError:
        HAS_TOML = False


VALID_INTERFACE_TYPES = ['sb', 'axi', 'axil', 'apb', 'gpio']

VALID_DIRECTIONS = ['input', 'output', 'manager', 'subordinate', 'inout']

VALID_POLICIES = ['strict', 'superset', 'relaxed']

VALID_PORT_TYPES = ['clock', 'reset', 'data']

VALID_PORT_DIRECTIONS = ['input', 'output', 'inout']


class PRConfig:
    """
    Loader and validator for PR configuration files.

    Supports YAML (recommended), TOML, and JSON formats.

    Attributes
    ----------
    version : str
        Configuration schema version
    simulation : dict
        Global simulation settings (tool, trace, frequency, etc.)
    static_region : dict or None
        Static region configuration (design, sources, parameters)
    partitions : list
        List of partition configurations
    reconfigurable_modules : list
        List of RM configurations
    port_compatibility_rules : dict
        Port compatibility policy and rules
    """

    SUPPORTED_VERSIONS = ['1.0']
    REQUIRED_FIELDS = ['partitions', 'reconfigurable_modules']

    def __init__(self):
        """Initialize empty configuration."""
        self.version: str = '1.0'
        self.simulation: Dict[str, Any] = {}
        self.static_region: Optional[Dict[str, Any]] = None
        self.partitions: List[Dict[str, Any]] = []
        self.reconfigurable_modules: List[Dict[str, Any]] = []
        self.port_compatibility_rules: Dict[str, Any] = {}
        self._source_path: Optional[Path] = None

    @classmethod
    def load(cls, path: Union[str, Path]) -> 'PRConfig':
        """
        Load configuration from file.

        Parameters
        ----------
        path : str or Path
            Path to configuration file (.yaml, .yml, .toml, or .json)

        Returns
        -------
        PRConfig
            Loaded and validated configuration

        Raises
        ------
        PRConfigError
            If file not found, format unsupported, or validation fails
        """
        path = Path(path)

        if not path.exists():
            raise PRConfigError(f"Configuration file not found: {path}")

        suffix = path.suffix.lower()

        with open(path, 'r') as f:
            if suffix in ['.yaml', '.yml']:
                if not HAS_YAML:
                    raise PRConfigError(
                        "PyYAML not installed. Install with: pip install pyyaml"
                    )
                data = yaml.safe_load(f)
            elif suffix == '.toml':
                if not HAS_TOML:
                    raise PRConfigError(
                        "TOML support not available. Use Python 3.11+ or install tomli"
                    )
                content = f.read()
                data = tomllib.loads(content)
            elif suffix == '.json':
                data = json.load(f)
            else:
                raise PRConfigError(
                    f"Unsupported config format: {suffix}. "
                    "Use .yaml, .yml, .toml, or .json"
                )

        config = cls.from_dict(data)
        config._source_path = path
        return config

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PRConfig':
        """
        Create configuration from dictionary.

        Parameters
        ----------
        data : dict
            Configuration dictionary

        Returns
        -------
        PRConfig
            Validated configuration object

        Raises
        ------
        PRConfigError
            If validation fails
        """
        config = cls()
        config._validate_and_load(data)
        return config

    def _validate_and_load(self, data: Dict[str, Any]):
        """Validate and load configuration data."""
        if not isinstance(data, dict):
            raise PRConfigError("Configuration must be a dictionary")

        self.version = str(data.get('version', '1.0'))
        if self.version not in self.SUPPORTED_VERSIONS:
            raise PRConfigError(
                f"Unsupported config version: {self.version}. "
                f"Supported: {self.SUPPORTED_VERSIONS}"
            )

        for field in self.REQUIRED_FIELDS:
            if field not in data:
                raise PRConfigError(f"Missing required field: '{field}'")
        self.simulation = data.get('simulation', {})
        self.static_region = data.get('static_region')
        self.partitions = data.get('partitions', [])
        self.reconfigurable_modules = data.get('reconfigurable_modules', [])
        self.port_compatibility_rules = data.get('port_compatibility_rules', {})

        self._apply_defaults()

        self._validate_simulation()

        if self.static_region:
            self._validate_static_region()

        self._validate_partitions()

        self._validate_rms()

        self._validate_compatibility_rules()

    def _apply_defaults(self):
        """
        Apply smart defaults to reduce YAML verbosity.

        This method infers values that can be reasonably defaulted:
        - RM 'design' defaults to RM 'name'
        - Port mapping auto-generated when partition and RM port names match
        - Default clocks: ['clk']
        - Default resets: [{'name': 'rst_n', 'polarity': 'negative'}]
        - Partition mapping auto-derived from interface naming convention

        After this, a minimal config like:
            - name: counter_rm
              partition: rp0
              sources: [rtl/counter_rm.sv]

        Becomes fully specified.
        """
        DEFAULT_CLOCKS = ['clk']
        DEFAULT_RESETS = [{'name': 'rst_n', 'polarity': 'negative'}]

        if self.static_region:
            if 'design' not in self.static_region:
                self.static_region['design'] = self.static_region.get('name', 'static_region')

            if 'clocks' not in self.static_region:
                awc = self.static_region.get('auto_wrap_config', {})
                if 'clock_name' in awc:
                    self.static_region['clocks'] = [awc['clock_name']]
                else:
                    self.static_region['clocks'] = DEFAULT_CLOCKS
            if 'resets' not in self.static_region:
                self.static_region['resets'] = DEFAULT_RESETS
        partition_interfaces = {}
        for part in self.partitions:
            partition_interfaces[part['name']] = part.get('interface', {})

        for rm in self.reconfigurable_modules:
            if 'design' not in rm:
                rm['design'] = rm['name']
            if 'clocks' not in rm:
                rm['clocks'] = DEFAULT_CLOCKS

            if 'resets' not in rm:
                rm['resets'] = DEFAULT_RESETS

            if 'port_mapping' not in rm:
                partition_name = rm.get('partition')
                if partition_name and partition_name in partition_interfaces:
                    part_intf = partition_interfaces[partition_name]
                    rm['port_mapping'] = {port: port for port in part_intf}

        self._derive_partition_mapping()

    def _derive_partition_mapping(self):
        """
        Auto-derive partition_mapping from static_region.interfaces.

        This eliminates hardcoded if/elif chains in system.py by deriving
        the mapping from interface naming conventions:

        Convention:
        - First partition (rp0): ext_req/ext_resp for external, rp0_req/rp0_resp for partition
        - Other partitions (rp1, rp2, ...): ext{N}_req/ext{N}_resp for external
        - Isolation bits assigned sequentially (rp0=0, rp1=1, ...)

        The mapping is stored in static_region['partition_mapping'] as:
        {
            'rp0': {
                'external_req': 'ext_req',
                'external_resp': 'ext_resp',
                'partition_req': 'rp0_req',
                'partition_resp': 'rp0_resp',
                'isolation_bit': 0
            },
            ...
        }
        """
        if not self.static_region:
            return

        if 'partition_mapping' in self.static_region:
            return

        interfaces = self.static_region.get('interfaces', {})
        if not interfaces:
            return

        partition_names = [p['name'] for p in self.partitions]
        mapping = {}

        for idx, part_name in enumerate(partition_names):
            part_req = f'{part_name}_req'
            part_resp = f'{part_name}_resp'

            if part_req not in interfaces or part_resp not in interfaces:
                continue

            if idx == 0:
                ext_req, ext_resp = 'ext_req', 'ext_resp'
            else:
                num = ''.join(filter(str.isdigit, part_name))
                if num:
                    ext_req = f'ext{num}_req'
                    ext_resp = f'ext{num}_resp'
                else:
                    ext_req = f'ext{idx}_req'
                    ext_resp = f'ext{idx}_resp'

            if ext_req in interfaces and ext_resp in interfaces:
                mapping[part_name] = {
                    'external_req': ext_req,
                    'external_resp': ext_resp,
                    'partition_req': part_req,
                    'partition_resp': part_resp,
                    'isolation_bit': idx
                }

        if mapping:
            self.static_region['partition_mapping'] = mapping

    def _validate_simulation(self):
        """Validate simulation settings."""
        sim = self.simulation

        if 'tool' in sim:
            if sim['tool'] not in ['verilator', 'icarus']:
                raise PRConfigError(
                    f"Invalid simulation tool: {sim['tool']}. "
                    "Use 'verilator' or 'icarus'"
                )

        if 'trace_type' in sim:
            if sim['trace_type'] not in ['vcd', 'fst']:
                raise PRConfigError(
                    f"Invalid trace_type: {sim['trace_type']}. "
                    "Use 'vcd' or 'fst'"
                )

        if 'frequency' in sim:
            try:
                freq = float(sim['frequency'])
                if freq <= 0:
                    raise ValueError()
            except (ValueError, TypeError):
                raise PRConfigError(
                    f"Invalid frequency: {sim['frequency']}. "
                    "Must be positive number"
                )

    def _validate_static_region(self):
        """Validate static region configuration."""
        sr = self.static_region

        if 'name' not in sr:
            raise PRConfigError("static_region missing 'name' field")

        if 'design' not in sr and 'sources' not in sr:
            raise PRConfigError(
                "static_region must have 'design' or 'sources' field"
            )

        if 'sources' in sr:
            if not isinstance(sr['sources'], list):
                raise PRConfigError("static_region 'sources' must be a list")

        if 'auto_wrap' in sr:
            if not isinstance(sr['auto_wrap'], bool):
                raise PRConfigError("static_region 'auto_wrap' must be a boolean")

        if 'auto_wrap_config' in sr:
            if not isinstance(sr['auto_wrap_config'], dict):
                raise PRConfigError(
                    "static_region 'auto_wrap_config' must be a dictionary"
                )
            valid_keys = {
                'enable_read_back', 'enable_interrupts', 'enable_wide_burst',
                'enable_inout', 'address_width', 'register_width',
                'clock_name', 'reset_name', 'reset_active_low'
            }
            for key in sr['auto_wrap_config']:
                if key not in valid_keys:
                    raise PRConfigError(
                        f"static_region unknown auto_wrap_config key: '{key}'. "
                        f"Valid keys: {valid_keys}"
                    )

        if 'ports' in sr:
            self._validate_ports_override(sr['ports'], "static_region")

    def _validate_partitions(self):
        """Validate partition configurations."""
        partition_names = set()

        for i, part in enumerate(self.partitions):
            if not isinstance(part, dict):
                raise PRConfigError(f"Partition {i} must be a dictionary")

            if 'name' not in part:
                raise PRConfigError(f"Partition {i} missing 'name' field")

            name = part['name']
            if name in partition_names:
                raise PRConfigError(f"Duplicate partition name: '{name}'")
            partition_names.add(name)

            has_interface = 'interface' in part
            has_boundary = 'boundary' in part

            if not has_interface and not has_boundary:
                raise PRConfigError(
                    f"Partition '{name}' must have 'interface' or 'boundary' field"
                )

            if has_interface:
                self._validate_interface(part['interface'], f"partition '{name}'")

            if has_boundary:
                self._validate_boundary(part['boundary'], f"partition '{name}'")

    def _validate_rms(self):
        """Validate RM configurations."""
        rm_names = set()
        partition_names = {p['name'] for p in self.partitions}

        for i, rm in enumerate(self.reconfigurable_modules):
            if not isinstance(rm, dict):
                raise PRConfigError(f"RM {i} must be a dictionary")

            if 'name' not in rm:
                raise PRConfigError(f"RM {i} missing 'name' field")

            name = rm['name']
            if name in rm_names:
                raise PRConfigError(f"Duplicate RM name: '{name}'")
            rm_names.add(name)

            if 'partition' not in rm:
                raise PRConfigError(f"RM '{name}' missing 'partition' field")

            if rm['partition'] not in partition_names:
                raise PRConfigError(
                    f"RM '{name}' references unknown partition: '{rm['partition']}'"
                )

            if 'design' not in rm and 'sources' not in rm:
                raise PRConfigError(
                    f"RM '{name}' must have 'design' or 'sources' field"
                )

            if 'sources' in rm:
                if not isinstance(rm['sources'], list):
                    raise PRConfigError(f"RM '{name}' 'sources' must be a list")

            if 'port_mapping' in rm:
                if not isinstance(rm['port_mapping'], dict):
                    raise PRConfigError(
                        f"RM '{name}' 'port_mapping' must be a dictionary"
                    )

            if 'auto_wrap' in rm:
                if not isinstance(rm['auto_wrap'], bool):
                    raise PRConfigError(
                        f"RM '{name}' 'auto_wrap' must be a boolean"
                    )

            if 'auto_wrap_config' in rm:
                if not isinstance(rm['auto_wrap_config'], dict):
                    raise PRConfigError(
                        f"RM '{name}' 'auto_wrap_config' must be a dictionary"
                    )
                valid_keys = {
                    'enable_read_back', 'enable_interrupts', 'enable_wide_burst',
                    'enable_inout', 'address_width', 'register_width',
                    'clock_name', 'reset_name', 'reset_active_low'
                }
                for key in rm['auto_wrap_config']:
                    if key not in valid_keys:
                        raise PRConfigError(
                            f"RM '{name}' unknown auto_wrap_config key: '{key}'. "
                            f"Valid keys: {valid_keys}"
                        )

            if 'ports' in rm:
                self._validate_ports_override(rm['ports'], f"RM '{name}'")

    def _validate_interface(self, interface: Dict, context: str):
        """Validate interface definition."""
        if not isinstance(interface, dict):
            raise PRConfigError(f"Interface for {context} must be a dictionary")

        for port_name, port_def in interface.items():
            if not isinstance(port_def, dict):
                raise PRConfigError(
                    f"Port '{port_name}' in {context} must be a dictionary"
                )

            if 'type' not in port_def:
                raise PRConfigError(
                    f"Port '{port_name}' in {context} missing 'type'"
                )

            if port_def['type'] not in VALID_INTERFACE_TYPES:
                raise PRConfigError(
                    f"Invalid port type '{port_def['type']}' for "
                    f"'{port_name}' in {context}. "
                    f"Valid types: {VALID_INTERFACE_TYPES}"
                )

            if 'direction' not in port_def:
                raise PRConfigError(
                    f"Port '{port_name}' in {context} missing 'direction'"
                )

            if port_def['direction'] not in VALID_DIRECTIONS:
                raise PRConfigError(
                    f"Invalid direction '{port_def['direction']}' for "
                    f"'{port_name}' in {context}. "
                    f"Valid directions: {VALID_DIRECTIONS}"
                )

    def _validate_boundary(self, boundary: list, context: str):
        """
        Validate partition boundary definition.

        Boundary defines the signals that cross the partition boundary
        between static region and RM. Each boundary port specifies:
        - name: signal name
        - direction: 'to_rm' (static→RM) or 'from_rm' (RM→static)
        - width: bit width (default 1)
        """
        if not isinstance(boundary, list):
            raise PRConfigError(f"Boundary for {context} must be a list")

        valid_directions = ['to_rm', 'from_rm']
        port_names = set()

        for i, port in enumerate(boundary):
            if not isinstance(port, dict):
                raise PRConfigError(
                    f"Boundary port {i} in {context} must be a dictionary"
                )

            if 'name' not in port:
                raise PRConfigError(
                    f"Boundary port {i} in {context} missing 'name'"
                )

            name = port['name']
            if name in port_names:
                raise PRConfigError(
                    f"Duplicate boundary port name '{name}' in {context}"
                )
            port_names.add(name)

            if 'direction' not in port:
                raise PRConfigError(
                    f"Boundary port '{name}' in {context} missing 'direction'"
                )

            if port['direction'] not in valid_directions:
                raise PRConfigError(
                    f"Invalid direction '{port['direction']}' for boundary port "
                    f"'{name}' in {context}. Valid: {valid_directions}"
                )

            if 'width' in port and not isinstance(port['width'], int):
                raise PRConfigError(
                    f"Boundary port '{name}' width must be an integer"
                )

    def _validate_ports_override(self, ports: Dict, context: str):
        """
        Validate explicit port definitions (override for RTL parsing).

        Ports override allows users to explicitly specify module port definitions
        instead of relying on pyslang RTL parsing. This is useful when:
        - pyslang cannot parse the RTL (unsupported constructs)
        - User wants to exclude certain ports from wrapping
        - User wants to override inferred port types (clock, reset, data)

        Expected format:
            ports:
              clock: {direction: input, type: clock}
              counter: {direction: input, width: 32}
              led_two_on: {direction: output}
              led_three_on: {direction: output, width: 1}

        Parameters
        ----------
        ports : dict
            Port definitions dictionary
        context : str
            Context string for error messages (e.g., "RM 'blinking_led'")
        """
        if not isinstance(ports, dict):
            raise PRConfigError(f"{context} 'ports' must be a dictionary")

        for port_name, port_def in ports.items():
            if not isinstance(port_name, str):
                raise PRConfigError(
                    f"{context} port name must be a string, got: {type(port_name)}"
                )

            if not isinstance(port_def, dict):
                raise PRConfigError(
                    f"{context} port '{port_name}' must be a dictionary"
                )
            if 'direction' not in port_def:
                raise PRConfigError(
                    f"{context} port '{port_name}' missing 'direction'"
                )

            if port_def['direction'] not in VALID_PORT_DIRECTIONS:
                raise PRConfigError(
                    f"{context} port '{port_name}' invalid direction "
                    f"'{port_def['direction']}'. Valid: {VALID_PORT_DIRECTIONS}"
                )

            if 'type' in port_def:
                if port_def['type'] not in VALID_PORT_TYPES:
                    raise PRConfigError(
                        f"{context} port '{port_name}' invalid type "
                        f"'{port_def['type']}'. Valid: {VALID_PORT_TYPES}"
                    )

            if 'width' in port_def:
                try:
                    width = int(port_def['width'])
                    if width < 1:
                        raise ValueError()
                except (ValueError, TypeError):
                    raise PRConfigError(
                        f"{context} port '{port_name}' 'width' must be "
                        f"a positive integer, got: {port_def['width']}"
                    )

    def _validate_compatibility_rules(self):
        """Validate port compatibility rules."""
        rules = self.port_compatibility_rules

        if 'default_policy' in rules:
            if rules['default_policy'] not in VALID_POLICIES:
                raise PRConfigError(
                    f"Invalid default_policy: {rules['default_policy']}. "
                    f"Valid policies: {VALID_POLICIES}"
                )

    def get_partition(self, name: str) -> Optional[Dict[str, Any]]:
        """Get partition configuration by name."""
        for part in self.partitions:
            if part['name'] == name:
                return part
        return None

    def get_rm(self, name: str) -> Optional[Dict[str, Any]]:
        """Get RM configuration by name."""
        for rm in self.reconfigurable_modules:
            if rm['name'] == name:
                return rm
        return None

    def get_rms_for_partition(self, partition_name: str) -> List[Dict[str, Any]]:
        """Get all RMs configured for a partition."""
        return [
            rm for rm in self.reconfigurable_modules
            if rm['partition'] == partition_name
        ]

    def get_initial_rm(self, partition_name: str) -> Optional[str]:
        """Get initial RM name for a partition."""
        part = self.get_partition(partition_name)
        if part:
            return part.get('initial_rm')
        return None

    def to_dict(self) -> Dict[str, Any]:
        """Convert configuration to dictionary."""
        result = {
            'version': self.version,
            'partitions': copy.deepcopy(self.partitions),
            'reconfigurable_modules': copy.deepcopy(self.reconfigurable_modules),
        }

        if self.simulation:
            result['simulation'] = copy.deepcopy(self.simulation)

        if self.static_region:
            result['static_region'] = copy.deepcopy(self.static_region)

        if self.port_compatibility_rules:
            result['port_compatibility_rules'] = copy.deepcopy(
                self.port_compatibility_rules
            )

        return result

    def save(self, path: Union[str, Path]):
        """
        Save configuration to file.

        Parameters
        ----------
        path : str or Path
            Output path (.yaml, .yml, .toml, or .json)
        """
        path = Path(path)
        data = self.to_dict()
        suffix = path.suffix.lower()

        with open(path, 'w') as f:
            if suffix in ['.yaml', '.yml']:
                if not HAS_YAML:
                    raise PRConfigError("PyYAML not installed")
                yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
            elif suffix == '.json':
                json.dump(data, f, indent=2)
            else:
                raise PRConfigError(
                    f"Cannot save to format: {suffix}. Use .yaml or .json"
                )
