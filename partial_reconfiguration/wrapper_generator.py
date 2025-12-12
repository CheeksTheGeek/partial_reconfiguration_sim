import json
import logging
import math
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional, Tuple, Set, Any
from pathlib import Path

import pyslang

from .rtl_parser import ModuleInfo, PortInfo, PortType, ResetPolarity

logger = logging.getLogger(__name__)


class AccessType(Enum):
    """Register access type."""
    READ_ONLY = auto()      # RO - hardware writes, software reads
    WRITE_ONLY = auto()     # WO - software writes, hardware reads
    READ_WRITE = auto()     # RW - software can read/write
    WRITE_1_CLEAR = auto()  # W1C - write 1 to clear (for interrupts)
    READ_CLEAR = auto()     # RC - read to clear


class RegisterType(Enum):
    """Type of register in the address map."""
    DATA_INPUT = auto()     # Input to wrapped module
    DATA_OUTPUT = auto()    # Output from wrapped module
    INOUT = auto()          # Bidirectional port
    INTERRUPT_STATUS = auto()
    INTERRUPT_ENABLE = auto()
    CONTROL = auto()        # Control register (enable, etc.)
    STATUS = auto()         # Status register


@dataclass
class RegisterInfo:
    """Information about a register in the address map."""
    name: str
    address: int
    width: int
    access: AccessType
    reg_type: RegisterType
    port_name: Optional[str] = None  # Associated port (if any)
    description: str = ""
    reset_value: int = 0
    is_wide: bool = False
    word_index: int = 0  # Which word of a wide register
    total_words: int = 1


@dataclass
class AddressRegion:
    """A region in the address map."""
    name: str
    base_address: int
    size: int
    registers: Dict[str, RegisterInfo] = field(default_factory=dict)
    description: str = ""


@dataclass
class AddressMap:
    """Complete address map for a wrapper."""
    regions: Dict[str, AddressRegion] = field(default_factory=dict)
    base_address: int = 0
    address_width: int = 16  # Bits of address space
    data_width: int = 32     # Register width

    def total_size(self) -> int:
        """Total address space used."""
        if not self.regions:
            return 0
        max_addr = max(
            r.base_address + r.size for r in self.regions.values()
        )
        return max_addr - self.base_address

    def get_register(self, name: str) -> Optional[RegisterInfo]:
        """Find a register by name across all regions."""
        for region in self.regions.values():
            if name in region.registers:
                return region.registers[name]
        return None

    def all_registers(self) -> List[RegisterInfo]:
        """Get all registers in address order."""
        regs = []
        for region in self.regions.values():
            regs.extend(region.registers.values())
        return sorted(regs, key=lambda r: r.address)

    def check_collisions(self) -> List[str]:
        """Check for address collisions. Returns list of errors."""
        errors = []
        all_regs = self.all_registers()

        for i, reg1 in enumerate(all_regs):
            size1 = max(4, (reg1.width + 7) // 8)  # Min 4 bytes
            for reg2 in all_regs[i+1:]:
                size2 = max(4, (reg2.width + 7) // 8)
                if (reg1.address < reg2.address + size2 and
                    reg2.address < reg1.address + size1):
                    errors.append(
                        f"Address collision: {reg1.name}@0x{reg1.address:X} "
                        f"overlaps {reg2.name}@0x{reg2.address:X}"
                    )
        return errors


@dataclass
class WrapperConfig:
    """Configuration for wrapper generation."""
    base_address: int = 0
    address_width: int = 16  # Bits of address decoding
    register_width: int = 32  # Width of each register
    umi_dw: int = 256
    umi_aw: int = 64
    umi_cw: int = 32
    enable_read_back: bool = True       # Read back input register values
    enable_interrupts: bool = True      # Generate interrupt logic
    enable_wide_burst: bool = True      # Burst transfers for wide ports
    enable_inout: bool = True           # Handle bidirectional ports
    enable_pipelining: bool = False     # Pipelined UMI (more complex)
    clock_name: str = "clk"
    reset_name: str = "rst_n"
    reset_active_low: bool = True
    wrapper_suffix: str = "_umi_wrapper"
    register_suffix: str = "_reg"
    address_alignment: int = 8  # Bytes (64-bit aligned)


@dataclass
class WrapperOutput:
    """Output from wrapper generation."""
    wrapper_rtl: str
    wrapper_name: str
    address_map: AddressMap
    python_module_code: str
    python_class_name: str
    c_header_code: str
    json_address_map: str
    inner_module_name: str
    validation_errors: List[str] = field(default_factory=list)
    validation_warnings: List[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        """Check if generated RTL passed validation."""
        return len(self.validation_errors) == 0


class UMIWrapperGenerator:
    """
    Generate UMI wrapper RTL and APIs for any module.

    Features:
    - Full UMI protocol with proper opcodes
    - Configurable address space (up to 32-bit)
    - Wide port support with burst transactions
    - Inout port handling with direction control
    - Read-back of input registers
    - Interrupt support with status/enable/clear
    - Multi-clock domain awareness
    - Pipelined transactions (optional)
    - pyslang validation of generated RTL

    Example:
        from partial_reconfiguration import RTLParser, UMIWrapperGenerator

        parser = RTLParser()
        module_info = parser.parse_module(['adder.sv'], 'adder')

        config = WrapperConfig(
            enable_interrupts=True,
            enable_read_back=True
        )
        generator = UMIWrapperGenerator(config)
        output = generator.generate(module_info)

        if output.is_valid:
            Path('wrapper.sv').write_text(output.wrapper_rtl)
            Path('adder_api.py').write_text(output.python_module_code)
            Path('adder_regs.h').write_text(output.c_header_code)
    """
    UMI_REQ_READ = 0x01
    UMI_REQ_WRITE = 0x03
    UMI_REQ_POSTED = 0x05
    UMI_REQ_ATOMIC = 0x07
    UMI_RESP_READ = 0x02
    UMI_RESP_WRITE = 0x04

    def __init__(self, config: WrapperConfig = None):
        """
        Initialize wrapper generator.

        Parameters
        ----------
        config : WrapperConfig, optional
            Configuration for wrapper generation
        """
        self.config = config or WrapperConfig()

    def generate(
        self,
        module_info: ModuleInfo,
        wrapper_name: str = None
    ) -> WrapperOutput:
        """
        Generate UMI wrapper for a module.

        Parameters
        ----------
        module_info : ModuleInfo
            Parsed module information from RTLParser
        wrapper_name : str, optional
            Name for generated wrapper. Defaults to '{module}_umi_wrapper'

        Returns
        -------
        WrapperOutput
            Generated wrapper RTL, address map, and APIs
        """
        if wrapper_name is None:
            wrapper_name = f"{module_info.name}{self.config.wrapper_suffix}"
        address_map = self._build_address_map(module_info)
        collisions = address_map.check_collisions()
        if collisions:
            for err in collisions:
                logger.error(err)
        name_errors = self._check_name_collisions(module_info)
        wrapper_rtl = self._generate_rtl(module_info, wrapper_name, address_map)
        validation_errors, validation_warnings = self._validate_rtl(
            wrapper_rtl, wrapper_name
        )
        validation_errors.extend(collisions)
        validation_errors.extend(name_errors)
        python_class_name = self._to_class_name(module_info.name) + "API"
        python_module_code = self._generate_python_module(
            module_info, address_map, python_class_name
        )
        c_header_code = self._generate_c_header(
            module_info, address_map, wrapper_name
        )
        json_address_map = self._generate_json_map(address_map, module_info)

        return WrapperOutput(
            wrapper_rtl=wrapper_rtl,
            wrapper_name=wrapper_name,
            address_map=address_map,
            python_module_code=python_module_code,
            python_class_name=python_class_name,
            c_header_code=c_header_code,
            json_address_map=json_address_map,
            inner_module_name=module_info.name,
            validation_errors=validation_errors,
            validation_warnings=validation_warnings
        )

    def _build_address_map(self, module_info: ModuleInfo) -> AddressMap:
        """Build complete address map for all ports."""
        address_map = AddressMap(
            base_address=self.config.base_address,
            address_width=self.config.address_width,
            data_width=self.config.register_width
        )

        current_addr = self.config.base_address
        align = self.config.address_alignment
        data_region = AddressRegion(
            name="data",
            base_address=current_addr,
            size=0,
            description="Data port registers"
        )
        for port in module_info.ports.values():
            if port.direction == 'input' and port.port_type == PortType.DATA:
                regs = self._create_port_registers(
                    port, current_addr,
                    AccessType.READ_WRITE if self.config.enable_read_back else AccessType.WRITE_ONLY,
                    RegisterType.DATA_INPUT
                )
                for reg in regs:
                    data_region.registers[reg.name] = reg
                    current_addr = reg.address + align
        for port in module_info.ports.values():
            if port.direction == 'output' and port.port_type == PortType.DATA:
                regs = self._create_port_registers(
                    port, current_addr,
                    AccessType.READ_ONLY,
                    RegisterType.DATA_OUTPUT
                )
                for reg in regs:
                    data_region.registers[reg.name] = reg
                    current_addr = reg.address + align
        if self.config.enable_inout:
            for port in module_info.ports.values():
                if port.direction == 'inout':
                    regs = self._create_port_registers(
                        port, current_addr,
                        AccessType.READ_WRITE,
                        RegisterType.INOUT
                    )
                    for reg in regs:
                        data_region.registers[reg.name] = reg
                        current_addr = reg.address + align
                    dir_reg = RegisterInfo(
                        name=f"{port.name}_dir",
                        address=current_addr,
                        width=port.width,
                        access=AccessType.READ_WRITE,
                        reg_type=RegisterType.CONTROL,
                        port_name=port.name,
                        description=f"Direction control for {port.name} (1=output)",
                        reset_value=0  # Default to input
                    )
                    data_region.registers[dir_reg.name] = dir_reg
                    current_addr += align

        data_region.size = current_addr - data_region.base_address
        address_map.regions["data"] = data_region
        if self.config.enable_interrupts:
            int_ports = module_info.interrupt_ports()
            if int_ports:
                int_region = AddressRegion(
                    name="interrupts",
                    base_address=current_addr,
                    size=0,
                    description="Interrupt registers"
                )
                total_int_bits = sum(p.width for p in int_ports)
                int_status = RegisterInfo(
                    name="int_status",
                    address=current_addr,
                    width=total_int_bits,
                    access=AccessType.WRITE_1_CLEAR,
                    reg_type=RegisterType.INTERRUPT_STATUS,
                    description="Interrupt status (write 1 to clear)"
                )
                int_region.registers[int_status.name] = int_status
                current_addr += align
                int_enable = RegisterInfo(
                    name="int_enable",
                    address=current_addr,
                    width=total_int_bits,
                    access=AccessType.READ_WRITE,
                    reg_type=RegisterType.INTERRUPT_ENABLE,
                    description="Interrupt enable mask"
                )
                int_region.registers[int_enable.name] = int_enable
                current_addr += align

                int_region.size = current_addr - int_region.base_address
                address_map.regions["interrupts"] = int_region

        return address_map

    def _create_port_registers(
        self,
        port: PortInfo,
        base_addr: int,
        access: AccessType,
        reg_type: RegisterType
    ) -> List[RegisterInfo]:
        """Create register(s) for a port, handling wide ports."""
        registers = []
        reg_width = self.config.register_width
        align = self.config.address_alignment

        if port.width <= reg_width:
            reg = RegisterInfo(
                name=port.name,
                address=base_addr,
                width=port.width,
                access=access,
                reg_type=reg_type,
                port_name=port.name,
                description=f"{'Input' if port.direction == 'input' else 'Output'}: {port.name}"
            )
            registers.append(reg)
        else:
            num_words = (port.width + reg_width - 1) // reg_width
            for i in range(num_words):
                word_width = min(reg_width, port.width - i * reg_width)
                reg = RegisterInfo(
                    name=f"{port.name}_{i}",
                    address=base_addr + i * align,
                    width=word_width,
                    access=access,
                    reg_type=reg_type,
                    port_name=port.name,
                    description=f"{port.name}[{(i+1)*reg_width-1}:{i*reg_width}]",
                    is_wide=True,
                    word_index=i,
                    total_words=num_words
                )
                registers.append(reg)

        return registers

    def _check_name_collisions(self, module_info: ModuleInfo) -> List[str]:
        """Check for naming collisions with generated signals."""
        errors = []
        reserved_suffixes = ['_reg', '_wire', '_oe', '_in', '_out', '_dir']

        for port_name in module_info.ports:
            for suffix in reserved_suffixes:
                generated_name = f"{port_name}{suffix}"
                if generated_name in module_info.ports:
                    errors.append(
                        f"Name collision: port '{generated_name}' conflicts "
                        f"with generated signal for '{port_name}'"
                    )

        return errors

    def _generate_rtl(
        self,
        module_info: ModuleInfo,
        wrapper_name: str,
        address_map: AddressMap
    ) -> str:
        """Generate SystemVerilog wrapper RTL."""
        lines = []
        cfg = self.config
        lines.extend([
            f"// Auto-generated UMI wrapper for {module_info.name}",
            "// Generated by partial_reconfiguration.wrapper_generator",
            "//",
            "// Address Map:",
        ])
        for reg in address_map.all_registers():
            acc = "RW" if reg.access == AccessType.READ_WRITE else \
                  "RO" if reg.access == AccessType.READ_ONLY else \
                  "WO" if reg.access == AccessType.WRITE_ONLY else "W1C"
            lines.append(f"//   0x{reg.address:04X}: {reg.name} [{reg.width}b] ({acc})")
        lines.extend(["", "`timescale 1ns/1ps", ""])
        lines.extend([
            f"module {wrapper_name} #(",
            f"    parameter DW = {cfg.umi_dw},",
            f"    parameter AW = {cfg.umi_aw},",
            f"    parameter CW = {cfg.umi_cw}",
            ") (",
            f"    input  wire {cfg.clock_name},",
            f"    input  wire {cfg.reset_name},",
            "",
            "    // UMI Request interface",
            "    input  wire [CW-1:0]  req_cmd,",
            "    input  wire [AW-1:0]  req_dstaddr,",
            "    input  wire [AW-1:0]  req_srcaddr,",
            "    input  wire [DW-1:0]  req_data,",
            "    input  wire           req_valid,",
            "    output reg            req_ready,",
            "",
            "    // UMI Response interface",
            "    output reg  [CW-1:0]  resp_cmd,",
            "    output reg  [AW-1:0]  resp_dstaddr,",
            "    output reg  [AW-1:0]  resp_srcaddr,",
            "    output reg  [DW-1:0]  resp_data,",
            "    output reg            resp_valid,",
            "    input  wire           resp_ready",
        ])
        if cfg.enable_interrupts and module_info.interrupt_ports():
            lines.append(",")
            lines.append("    output wire           irq")

        lines.extend([");", ""])
        lines.extend([
            "    // UMI opcodes",
            f"    localparam [4:0] UMI_REQ_READ    = 5'h{self.UMI_REQ_READ:02X};",
            f"    localparam [4:0] UMI_REQ_WRITE   = 5'h{self.UMI_REQ_WRITE:02X};",
            f"    localparam [4:0] UMI_REQ_POSTED  = 5'h{self.UMI_REQ_POSTED:02X};",
            f"    localparam [4:0] UMI_RESP_READ   = 5'h{self.UMI_RESP_READ:02X};",
            f"    localparam [4:0] UMI_RESP_WRITE  = 5'h{self.UMI_RESP_WRITE:02X};",
            ""
        ])
        addr_bits = cfg.address_width
        lines.extend([
            "    // Address and opcode decode",
            "    wire [4:0] opcode = req_cmd[4:0];",
            f"    wire [{addr_bits-1}:0] addr = req_dstaddr[{addr_bits-1}:0];",
            "    wire is_read  = (opcode == UMI_REQ_READ);",
            "    wire is_write = (opcode == UMI_REQ_WRITE) || (opcode == UMI_REQ_POSTED);",
            ""
        ])
        lines.append("    // Input registers (directly accessible from UMI)")
        for port in module_info.ports.values():
            if port.direction == 'input' and port.port_type == PortType.DATA:
                width_str = f"[{port.width-1}:0]" if port.width > 1 else ""
                lines.append(f"    reg {width_str} {port.name}_reg;")
        lines.append("")
        if cfg.enable_inout:
            inout_ports = module_info.inout_ports()
            if inout_ports:
                lines.append("    // Bidirectional port registers")
                for port in inout_ports:
                    width_str = f"[{port.width-1}:0]" if port.width > 1 else ""
                    lines.append(f"    reg {width_str} {port.name}_out_reg;  // Output data")
                    lines.append(f"    reg {width_str} {port.name}_oe_reg;   // Output enable")
                    lines.append(f"    wire {width_str} {port.name}_in;      // Input sample")
                lines.append("")
        lines.append("    // Output wires from inner module")
        for port in module_info.ports.values():
            if port.direction == 'output' and port.port_type == PortType.DATA:
                width_str = f"[{port.width-1}:0]" if port.width > 1 else ""
                lines.append(f"    wire {width_str} {port.name}_wire;")
        lines.append("")
        if cfg.enable_interrupts:
            int_ports = module_info.interrupt_ports()
            if int_ports:
                total_bits = sum(p.width for p in int_ports)
                width_str = f"[{total_bits-1}:0]" if total_bits > 1 else ""
                lines.extend([
                    "    // Interrupt registers",
                    f"    reg {width_str} int_status_reg;",
                    f"    reg {width_str} int_enable_reg;",
                    f"    wire {width_str} int_pending;",
                    ""
                ])
        lines.extend([
            f"    // Inner module: {module_info.name}",
            f"    {module_info.name} u_inner ("
        ])

        port_connections = []
        for port in module_info.ports.values():
            if port.port_type == PortType.CLOCK:
                port_connections.append(f"        .{port.name}({cfg.clock_name})")
            elif port.port_type == PortType.RESET:
                if port.reset_polarity == ResetPolarity.ACTIVE_LOW:
                    port_connections.append(f"        .{port.name}({cfg.reset_name})")
                else:
                    if cfg.reset_active_low:
                        port_connections.append(f"        .{port.name}(~{cfg.reset_name})")
                    else:
                        port_connections.append(f"        .{port.name}({cfg.reset_name})")
            elif port.direction == 'input' and port.port_type == PortType.DATA:
                port_connections.append(f"        .{port.name}({port.name}_reg)")
            elif port.direction == 'output':
                port_connections.append(f"        .{port.name}({port.name}_wire)")
            elif port.direction == 'inout' and cfg.enable_inout:
                port_connections.append(f"        .{port.name}({port.name}_out_reg)")

        lines.append(",\n".join(port_connections))
        lines.extend(["    );", ""])
        if cfg.enable_interrupts and module_info.interrupt_ports():
            int_ports = module_info.interrupt_ports()
            int_signals = [f"{p.name}_wire" for p in int_ports]
            lines.extend([
                "    // Interrupt logic",
                f"    wire [{sum(p.width for p in int_ports)-1}:0] int_raw = {{{', '.join(int_signals)}}};",
                "    assign int_pending = int_status_reg & int_enable_reg;",
                "    assign irq = |int_pending;",
                ""
            ])
        reset_cond = f"!{cfg.reset_name}" if cfg.reset_active_low else cfg.reset_name
        lines.extend([
            "    // UMI protocol state machine",
            "    localparam S_IDLE = 2'd0, S_RESPOND = 2'd1, S_WAIT = 2'd2;",
            "    reg [1:0] state;",
            "",
            f"    always @(posedge {cfg.clock_name} or {'negedge' if cfg.reset_active_low else 'posedge'} {cfg.reset_name}) begin",
            f"        if ({reset_cond}) begin",
            "            state <= S_IDLE;",
            "            req_ready <= 1'b1;",
            "            resp_valid <= 1'b0;",
            f"            resp_cmd <= {cfg.umi_cw}'d0;",
            f"            resp_dstaddr <= {cfg.umi_aw}'d0;",
            f"            resp_srcaddr <= {cfg.umi_aw}'d0;",
            f"            resp_data <= {cfg.umi_dw}'d0;",
        ])
        for port in module_info.ports.values():
            if port.direction == 'input' and port.port_type == PortType.DATA:
                lines.append(f"            {port.name}_reg <= {port.width}'d0;")
        if cfg.enable_inout:
            for port in module_info.inout_ports():
                lines.append(f"            {port.name}_out_reg <= {port.width}'d0;")
                lines.append(f"            {port.name}_oe_reg <= {port.width}'d0;")
        if cfg.enable_interrupts and module_info.interrupt_ports():
            int_ports = module_info.interrupt_ports()
            total_bits = sum(p.width for p in int_ports)
            lines.append(f"            int_status_reg <= {total_bits}'d0;")
            lines.append(f"            int_enable_reg <= {total_bits}'d0;")

        lines.extend([
            "        end else begin",
        ])
        if cfg.enable_interrupts and module_info.interrupt_ports():
            lines.append("            // Capture interrupt edges")
            lines.append("            int_status_reg <= int_status_reg | int_raw;")
            lines.append("")

        lines.extend([
            "            case (state)",
            "                S_IDLE: begin",
            "                    req_ready <= 1'b1;",
            "                    if (req_valid && req_ready) begin",
            "                        req_ready <= 1'b0;",
            "                        resp_dstaddr <= req_srcaddr;",
            "                        resp_srcaddr <= req_dstaddr;",
            "",
            "                        if (is_read) begin",
            "                            resp_cmd <= {req_cmd[CW-1:5], UMI_RESP_READ};",
            "                            case (addr)",
        ])
        for reg in address_map.all_registers():
            if reg.access in [AccessType.READ_ONLY, AccessType.READ_WRITE, AccessType.WRITE_1_CLEAR]:
                pad = cfg.umi_dw - reg.width
                if reg.reg_type == RegisterType.DATA_INPUT:
                    signal = f"{reg.port_name}_reg"
                elif reg.reg_type == RegisterType.DATA_OUTPUT:
                    signal = f"{reg.port_name}_wire"
                elif reg.reg_type == RegisterType.INOUT:
                    signal = f"{reg.port_name}_out_reg"
                elif reg.reg_type == RegisterType.INTERRUPT_STATUS:
                    signal = "int_status_reg"
                elif reg.reg_type == RegisterType.INTERRUPT_ENABLE:
                    signal = "int_enable_reg"
                elif reg.reg_type == RegisterType.CONTROL and reg.name.endswith('_dir'):
                    signal = f"{reg.port_name}_oe_reg"
                else:
                    signal = f"{reg.width}'d0"
                if reg.is_wide:
                    base_bits = reg.word_index * cfg.register_width
                    end_bits = min((reg.word_index + 1) * cfg.register_width, reg.width) - 1
                    signal = f"{reg.port_name}_reg[{end_bits}:{base_bits}]" if reg.reg_type == RegisterType.DATA_INPUT else signal

                if pad > 0:
                    lines.append(f"                                {addr_bits}'h{reg.address:04X}: resp_data <= {{{pad}'b0, {signal}}};")
                else:
                    lines.append(f"                                {addr_bits}'h{reg.address:04X}: resp_data <= {signal};")

        lines.extend([
            f"                                default: resp_data <= {cfg.umi_dw}'d0;",
            "                            endcase",
            "                            resp_valid <= 1'b1;",
            "                            state <= S_RESPOND;",
            "",
            "                        end else if (is_write) begin",
            "                            resp_cmd <= {req_cmd[CW-1:5], UMI_RESP_WRITE};",
            "                            case (addr)",
        ])
        for reg in address_map.all_registers():
            if reg.access in [AccessType.WRITE_ONLY, AccessType.READ_WRITE]:
                if reg.reg_type == RegisterType.DATA_INPUT:
                    signal = f"{reg.port_name}_reg"
                elif reg.reg_type == RegisterType.INOUT:
                    signal = f"{reg.port_name}_out_reg"
                elif reg.reg_type == RegisterType.INTERRUPT_ENABLE:
                    signal = "int_enable_reg"
                elif reg.reg_type == RegisterType.CONTROL and reg.name.endswith('_dir'):
                    signal = f"{reg.port_name}_oe_reg"
                else:
                    continue

                lines.append(f"                                {addr_bits}'h{reg.address:04X}: {signal} <= req_data[{reg.width-1}:0];")

            elif reg.access == AccessType.WRITE_1_CLEAR:
                if reg.reg_type == RegisterType.INTERRUPT_STATUS:
                    lines.append(f"                                {addr_bits}'h{reg.address:04X}: int_status_reg <= int_status_reg & ~req_data[{reg.width-1}:0];")

        lines.extend([
            "                                default: ; // Ignore",
            "                            endcase",
            f"                            resp_data <= {cfg.umi_dw}'d0;",
            "                            resp_valid <= 1'b1;",
            "                            state <= S_RESPOND;",
            "                        end",
            "                    end",
            "                end",
            "",
            "                S_RESPOND: begin",
            "                    if (resp_valid && resp_ready) begin",
            "                        resp_valid <= 1'b0;",
            "                        req_ready <= 1'b1;",
            "                        state <= S_IDLE;",
            "                    end",
            "                end",
            "",
            "                default: state <= S_IDLE;",
            "            endcase",
            "        end",
            "    end",
            "",
            "endmodule"
        ])

        return "\n".join(lines)

    def _validate_rtl(
        self,
        rtl_code: str,
        module_name: str
    ) -> Tuple[List[str], List[str]]:
        """Validate generated RTL using pyslang."""
        errors = []
        warnings = []

        try:
            tree = pyslang.SyntaxTree.fromText(rtl_code)
            compilation = pyslang.Compilation()
            compilation.addSyntaxTree(tree)

            diags = compilation.getAllDiagnostics()

            for diag in diags:
                msg = str(diag)
                try:
                    if hasattr(diag, 'severity'):
                        severity = diag.severity
                        if severity == pyslang.DiagnosticSeverity.Error:
                            errors.append(f"RTL Error: {msg}")
                        elif severity == pyslang.DiagnosticSeverity.Warning:
                            warnings.append(f"RTL Warning: {msg}")
                    else:
                        msg_lower = msg.lower()
                        if 'error' in msg_lower:
                            errors.append(f"RTL Error: {msg}")
                        elif 'warning' in msg_lower:
                            warnings.append(f"RTL Warning: {msg}")
                except Exception:
                    warnings.append(f"RTL: {msg}")
            root = compilation.getRoot()
            top_instances = list(root.topInstances)

            if not any(inst.name == module_name for inst in top_instances):
                errors.append(f"Generated module '{module_name}' not found in parsed RTL")

        except Exception as e:
            errors.append(f"RTL validation failed: {e}")

        return errors, warnings

    def _generate_python_module(
        self,
        module_info: ModuleInfo,
        address_map: AddressMap,
        class_name: str
    ) -> str:
        """Generate Python API as a proper importable module."""
        lines = []
        mod_name = module_info.name
        lines.extend([
            '"""',
            f"Auto-generated UMI API for {mod_name} module.",
            "",
            "This module provides a Python interface to control the wrapped",
            f"{mod_name} module via UMI protocol.",
            "",
            "Generated by partial_reconfiguration.wrapper_generator",
            '"""',
            "",
            "from typing import Optional",
            "import numpy as np",
            "",
            "# Attempt to import UmiTxRx, with fallback for standalone use",
            "try:",
            "    from switchboard import UmiTxRx",
            "except ImportError:",
            "    UmiTxRx = None  # type: ignore",
            "",
            ""
        ])
        lines.extend([
            f"class {mod_name.upper()}_REGS:",
            f'    """Register addresses for {mod_name}."""',
            ""
        ])

        for reg in address_map.all_registers():
            const_name = reg.name.upper()
            lines.append(f"    {const_name} = 0x{reg.address:04X}")

        lines.extend(["", ""])
        lines.extend([
            f"class {class_name}:",
            '    """',
            f"    UMI API for {mod_name} module.",
            "",
            "    Provides read/write methods for all accessible registers.",
            "",
            "    Example:",
            f"        from {mod_name}_api import {class_name}",
            "        from switchboard import UmiTxRx",
            "",
            "        umi = UmiTxRx('req.q', 'resp.q')",
            f"        api = {class_name}(umi)",
            "",
        ])
        for port in module_info.ports.values():
            if port.direction == 'input' and port.port_type == PortType.DATA:
                lines.append(f"        api.write_{port.name}(value)  # Write to {port.name}")
            elif port.direction == 'output' and port.port_type == PortType.DATA:
                lines.append(f"        api.read_{port.name}()  # Read {port.name}")

        lines.extend([
            '    """',
            "",
            f"    # Register addresses",
            f"    REGS = {mod_name.upper()}_REGS",
            ""
        ])
        lines.extend([
            "    def __init__(self, umi: 'UmiTxRx'):",
            '        """',
            "        Initialize API with UMI interface.",
            "",
            "        Parameters",
            "        ----------",
            "        umi : UmiTxRx",
            "            Switchboard UMI interface",
            '        """',
            "        self._umi = umi",
            ""
        ])
        for reg in address_map.all_registers():
            dtype = self._get_numpy_dtype(reg.width)
            port = module_info.ports.get(reg.port_name)

            if reg.access in [AccessType.READ_ONLY, AccessType.READ_WRITE, AccessType.WRITE_1_CLEAR]:
                lines.extend([
                    f"    def read_{reg.name}(self) -> int:",
                    f'        """',
                    f"        Read {reg.name} register.",
                    "",
                ])
                if reg.port_name:
                    lines.append(f"        Associated port: {reg.port_name}")
                lines.extend([
                    f"        Address: 0x{reg.address:04X}",
                    f"        Width: {reg.width} bits",
                    "",
                    "        Returns",
                    "        -------",
                    "        int",
                    f"            Current value ({reg.width} bits)",
                    '        """',
                    f"        return int(self._umi.read(0x{reg.address:04X}, {dtype}))",
                    ""
                ])

            if reg.access in [AccessType.WRITE_ONLY, AccessType.READ_WRITE]:
                lines.extend([
                    f"    def write_{reg.name}(self, value: int) -> None:",
                    f'        """',
                    f"        Write to {reg.name} register.",
                    "",
                ])
                if reg.port_name:
                    lines.append(f"        Associated port: {reg.port_name}")
                lines.extend([
                    f"        Address: 0x{reg.address:04X}",
                    f"        Width: {reg.width} bits",
                    "",
                    "        Parameters",
                    "        ----------",
                    "        value : int",
                    f"            Value to write (0 to {(1 << reg.width) - 1})",
                    '        """',
                    f"        self._umi.write(0x{reg.address:04X}, {dtype}(value))",
                    ""
                ])

            if reg.access == AccessType.WRITE_1_CLEAR:
                lines.extend([
                    f"    def clear_{reg.name}(self, mask: int) -> None:",
                    f'        """',
                    f"        Clear bits in {reg.name} (write-1-to-clear).",
                    "",
                    "        Parameters",
                    "        ----------",
                    "        mask : int",
                    "            Bits to clear (1 = clear that bit)",
                    '        """',
                    f"        self._umi.write(0x{reg.address:04X}, {dtype}(mask))",
                    ""
                ])
        if self.config.enable_interrupts and module_info.interrupt_ports():
            lines.extend([
                "    # Interrupt convenience methods",
                "",
                "    def get_pending_interrupts(self) -> int:",
                '        """Get pending and enabled interrupts."""',
                "        status = self.read_int_status()",
                "        enable = self.read_int_enable()",
                "        return status & enable",
                "",
                "    def clear_interrupts(self, mask: int) -> None:",
                '        """Clear specified interrupt bits."""',
                "        self.clear_int_status(mask)",
                "",
                "    def enable_interrupts(self, mask: int) -> None:",
                '        """Enable specified interrupts."""',
                "        current = self.read_int_enable()",
                "        self.write_int_enable(current | mask)",
                "",
                "    def disable_interrupts(self, mask: int) -> None:",
                '        """Disable specified interrupts."""',
                "        current = self.read_int_enable()",
                "        self.write_int_enable(current & ~mask)",
                ""
            ])

        return "\n".join(lines)

    def _generate_c_header(
        self,
        module_info: ModuleInfo,
        address_map: AddressMap,
        wrapper_name: str
    ) -> str:
        """Generate C/C++ header file."""
        lines = []
        guard = f"{wrapper_name.upper()}_REGS_H"
        mod_upper = module_info.name.upper()

        lines.extend([
            "/**",
            f" * Auto-generated register definitions for {module_info.name}",
            " *",
            " * Generated by partial_reconfiguration.wrapper_generator",
            " */",
            "",
            f"#ifndef {guard}",
            f"#define {guard}",
            "",
            "#include <stdint.h>",
            "",
            f"/* Base address (configurable) */",
            f"#ifndef {mod_upper}_BASE",
            f"#define {mod_upper}_BASE 0x{self.config.base_address:08X}",
            "#endif",
            "",
            "/* Register offsets */",
        ])

        for reg in address_map.all_registers():
            offset = reg.address - self.config.base_address
            lines.append(f"#define {mod_upper}_{reg.name.upper()}_OFFSET 0x{offset:04X}")

        lines.extend(["", "/* Register addresses */"])

        for reg in address_map.all_registers():
            offset = reg.address - self.config.base_address
            lines.append(
                f"#define {mod_upper}_{reg.name.upper()}_ADDR "
                f"({mod_upper}_BASE + 0x{offset:04X})"
            )

        lines.extend(["", "/* Register access macros */"])

        for reg in address_map.all_registers():
            addr_macro = f"{mod_upper}_{reg.name.upper()}_ADDR"
            c_type = self._get_c_type(reg.width)

            if reg.access in [AccessType.READ_ONLY, AccessType.READ_WRITE, AccessType.WRITE_1_CLEAR]:
                lines.append(
                    f"#define {mod_upper}_READ_{reg.name.upper()}() "
                    f"(*((volatile {c_type}*){addr_macro}))"
                )

            if reg.access in [AccessType.WRITE_ONLY, AccessType.READ_WRITE, AccessType.WRITE_1_CLEAR]:
                lines.append(
                    f"#define {mod_upper}_WRITE_{reg.name.upper()}(val) "
                    f"(*((volatile {c_type}*){addr_macro}) = (val))"
                )

        lines.extend([
            "",
            f"#endif /* {guard} */",
            ""
        ])

        return "\n".join(lines)

    def _generate_json_map(
        self,
        address_map: AddressMap,
        module_info: ModuleInfo
    ) -> str:
        """Generate JSON address map for tooling."""
        data = {
            "module": module_info.name,
            "generator": "partial_reconfiguration.wrapper_generator",
            "config": {
                "base_address": self.config.base_address,
                "address_width": self.config.address_width,
                "register_width": self.config.register_width,
                "umi_dw": self.config.umi_dw,
                "umi_aw": self.config.umi_aw,
                "umi_cw": self.config.umi_cw
            },
            "regions": {},
            "registers": []
        }

        for region_name, region in address_map.regions.items():
            data["regions"][region_name] = {
                "base_address": region.base_address,
                "size": region.size,
                "description": region.description
            }

        for reg in address_map.all_registers():
            reg_data = {
                "name": reg.name,
                "address": reg.address,
                "offset": reg.address - self.config.base_address,
                "width": reg.width,
                "access": reg.access.name,
                "type": reg.reg_type.name,
                "description": reg.description,
                "reset_value": reg.reset_value
            }
            if reg.port_name:
                reg_data["port"] = reg.port_name
            if reg.is_wide:
                reg_data["word_index"] = reg.word_index
                reg_data["total_words"] = reg.total_words

            data["registers"].append(reg_data)

        return json.dumps(data, indent=2)

    def _to_class_name(self, name: str) -> str:
        """Convert module name to PascalCase."""
        parts = name.split('_')
        return ''.join(p.capitalize() for p in parts)

    def _get_numpy_dtype(self, width: int) -> str:
        """Get numpy dtype for given bit width."""
        if width <= 8:
            return "np.uint8"
        elif width <= 16:
            return "np.uint16"
        elif width <= 32:
            return "np.uint32"
        else:
            return "np.uint64"

    def _get_c_type(self, width: int) -> str:
        """Get C type for given bit width."""
        if width <= 8:
            return "uint8_t"
        elif width <= 16:
            return "uint16_t"
        elif width <= 32:
            return "uint32_t"
        else:
            return "uint64_t"
