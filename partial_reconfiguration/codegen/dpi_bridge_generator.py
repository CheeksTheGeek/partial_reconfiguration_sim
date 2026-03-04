"""
DPI Bridge Generator for partition boundaries.

Generates two SystemVerilog modules per partition:
1. Static-side DPI bridge: Same port signature as the RM (drop-in replacement
   in the static region). Uses DPI-C function calls to read/write shared memory.
2. RM-side DPI wrapper: Wraps real RM with DPI channel I/O.

Width-aware: DPI types are selected based on actual port widths to avoid
Verilator WIDTHEXPAND/WIDTHTRUNC warnings. Ports are classified into
DPI type bands:
  - 1-32 bits  -> DPI `int`     (C++ `int`)
  - 33-64 bits -> DPI `longint` (C++ `long long`)
  - >64 bits   -> chunked into multiple 64-bit DPI calls
"""
from typing import List, Dict, Optional
from pathlib import Path
from dataclasses import dataclass, field
import logging

logger = logging.getLogger(__name__)


def num_chunks(width: int) -> int:
    """Number of 64-bit SHM slots needed for a port of the given width."""
    return (width + 63) // 64


@dataclass
class BoundaryPort:
    """A port on the partition boundary."""
    name: str
    width: int
    direction: str  # 'to_rm' or 'from_rm'
    clock: Optional[str] = None  # per-port clock override; None = use partition primary

    @property
    def num_chunks(self) -> int:
        return (self.width + 63) // 64


@dataclass
class PartitionBoundaryDef:
    """Definition of a partition boundary for DPI bridge generation."""
    partition_name: str
    rm_module_name: str
    ports: List[BoundaryPort]
    clock_names: List[str] = field(default_factory=lambda: ['clk'])
    reset_name: Optional[str] = None
    reset_polarity: str = 'negative'  # 'negative' (active-low) or 'positive'

    @property
    def clock_name(self) -> str:
        """Backward-compat: primary clock."""
        return self.clock_names[0]


def _dpi_type(width: int) -> str:
    """Select the DPI-C type for a chunk of the given bit width (max 64)."""
    if width <= 32:
        return 'int'
    else:
        return 'longint'


def _chunk_width(port_width: int, chunk_idx: int) -> int:
    """Return the effective bit width of a specific chunk."""
    lo = chunk_idx * 64
    hi = min(lo + 63, port_width - 1)
    return hi - lo + 1


def _sv_send_cast(port: BoundaryPort, chunk_idx: int = 0) -> str:
    """
    Generate the SV expression that widens a port value to the DPI type width.

    For exact-width matches (32 or 64), no cast needed.
    For narrower signals, zero-extend to the DPI type width.
    For wide ports (>64), extract the appropriate chunk slice.
    """
    lo = chunk_idx * 64
    hi = min(lo + 63, port.width - 1)
    cw = hi - lo + 1
    dpi = _dpi_type(cw)

    # Get the source expression (slice for wide ports, full name for narrow)
    if port.width > 64:
        src = f"{port.name}[{hi}:{lo}]"
    elif port.width > 1 and cw < port.width:
        src = f"{port.name}[{hi}:{lo}]"
    else:
        src = port.name

    if dpi == 'int':
        if cw == 32:
            return src
        pad = 32 - cw
        return f"int'({{{pad}'d0, {src}}})"
    else:  # longint
        if cw == 64:
            return src
        pad = 64 - cw
        return f"longint'({{{pad}'d0, {src}}})"


def _sv_recv_trunc(port: BoundaryPort, expr: str, chunk_idx: int = 0):
    """
    Generate the SV expression that truncates a DPI return value to port width.

    For wide ports (>64), returns (lhs_slice, rhs_expr) tuple.
    For narrow ports, returns the expression string.
    """
    lo = chunk_idx * 64
    hi = min(lo + 63, port.width - 1)
    cw = hi - lo + 1

    if port.width > 64:
        # Wide port: assign to a slice
        trunc = expr if cw in (32, 64) else f"{expr}[{cw - 1}:0]"
        return (f"{port.name}[{hi}:{lo}]", trunc)
    else:
        # Narrow port: original behavior
        dpi = _dpi_type(port.width)
        if dpi == 'int' and port.width == 32:
            return expr
        if dpi == 'longint' and port.width == 64:
            return expr
        return f"{expr}[{port.width - 1}:0]"


class DpiBridgeGenerator:
    """
    Generates DPI-based bridge modules for partition boundaries.

    For each partition, generates:
    1. Static-side bridge: same module name as RM, uses DPI-C send/recv
    2. RM-side DPI wrapper: wraps real RM, uses DPI-C send/recv

    All DPI function signatures use width-appropriate types selected by
    _dpi_type() to avoid Verilator width warnings.
    """

    def __init__(self, build_dir: str = None):
        self.build_dir = Path(build_dir) if build_dir else Path('build/pr')
        self.bridges_dir = self.build_dir / 'bridges'
        self.wrappers_dir = self.build_dir / 'wrappers'
        self.bridges_dir.mkdir(parents=True, exist_ok=True)
        self.wrappers_dir.mkdir(parents=True, exist_ok=True)

    def generate_static_side_bridge(self, boundary: PartitionBoundaryDef) -> Path:
        """
        Generate the static-side DPI bridge.

        This module has the same name and ports as the RM, so it acts as a
        drop-in replacement in the static region. Instead of real logic,
        it uses DPI-C functions to send inputs to and receive outputs from
        the RM running in another process.
        """
        part = boundary.partition_name
        module_name = boundary.rm_module_name
        to_rm = [p for p in boundary.ports if p.direction == 'to_rm']
        from_rm = [p for p in boundary.ports if p.direction == 'from_rm']

        lines = []
        lines.append(f"// Static-side DPI bridge for partition: {part}")
        lines.append(f"// Replaces {module_name} in static region")
        lines.append("// Generated by partial_reconfiguration DPI bridge generator")
        lines.append("")
        lines.append("`timescale 1ns/1ps")
        lines.append("")

        # Module declaration with same ports as RM
        lines.append(f"module {module_name} (")
        # Clock ports
        clock_port_lines = [f"    input wire {clk}" for clk in boundary.clock_names]
        # Reset port (if configured)
        if boundary.reset_name is not None:
            clock_port_lines.append(f"    input wire {boundary.reset_name}")
        port_lines = list(clock_port_lines)
        for port in boundary.ports:
            width_str = f"[{port.width-1}:0] " if port.width > 1 else ""
            if port.direction == 'to_rm':
                port_lines.append(f"    input wire {width_str}{port.name}")
            else:
                port_lines.append(f"    output reg {width_str}{port.name}")
        lines.append(",\n".join(port_lines))
        lines.append(");")
        lines.append("")

        # DPI imports — width-matched types, chunked for wide ports
        for port in to_rm:
            nc = port.num_chunks
            for c in range(nc):
                cw = _chunk_width(port.width, c)
                dt = _dpi_type(cw)
                suffix = f"_chunk{c}_send" if nc > 1 else "_send"
                lines.append(
                    f'    import "DPI-C" function void '
                    f'dpi_static_{part}_{port.name}{suffix}(input {dt} data);'
                )
        lines.append("")

        for port in from_rm:
            nc = port.num_chunks
            for c in range(nc):
                cw = _chunk_width(port.width, c)
                dt = _dpi_type(cw)
                suffix_d = f"_chunk{c}_recv_data" if nc > 1 else "_recv_data"
                suffix_v = f"_chunk{c}_recv_valid" if nc > 1 else "_recv_valid"
                lines.append(
                    f'    import "DPI-C" function {dt} '
                    f'dpi_static_{part}_{port.name}{suffix_d}();'
                )
                lines.append(
                    f'    import "DPI-C" function int '
                    f'dpi_static_{part}_{port.name}{suffix_v}();'
                )
        lines.append("")

        # Group ports by their effective clock
        def _port_clock(p):
            return p.clock or boundary.clock_names[0]

        # Send inputs to RM — grouped per clock
        if to_rm:
            clk_groups = {}
            for port in to_rm:
                clk = _port_clock(port)
                clk_groups.setdefault(clk, []).append(port)
            for clk, ports in clk_groups.items():
                lines.append(f"    // Send inputs to RM via DPI channels (clock: {clk})")
                lines.append(f"    always @(posedge {clk}) begin")
                for port in ports:
                    nc = port.num_chunks
                    for c in range(nc):
                        arg = _sv_send_cast(port, c)
                        suffix = f"_chunk{c}_send" if nc > 1 else "_send"
                        lines.append(f"        dpi_static_{part}_{port.name}{suffix}({arg});")
                lines.append("    end")
                lines.append("")

        # Receive outputs from RM — grouped per clock
        if from_rm:
            clk_groups = {}
            for port in from_rm:
                clk = _port_clock(port)
                clk_groups.setdefault(clk, []).append(port)
            for clk, ports in clk_groups.items():
                lines.append(f"    // Receive outputs from RM via DPI channels (clock: {clk})")
                lines.append(f"    always @(posedge {clk}) begin")
                for port in ports:
                    nc = port.num_chunks
                    for c in range(nc):
                        suffix_d = f"_chunk{c}_recv_data" if nc > 1 else "_recv_data"
                        suffix_v = f"_chunk{c}_recv_valid" if nc > 1 else "_recv_valid"
                        recv_fn = f"dpi_static_{part}_{port.name}{suffix_d}()"
                        result = _sv_recv_trunc(port, recv_fn, c)
                        valid_fn = f"dpi_static_{part}_{port.name}{suffix_v}()"
                        if isinstance(result, tuple):
                            lhs, rhs = result
                            lines.append(f"        if ({valid_fn})")
                            lines.append(f"            {lhs} <= {rhs};")
                        else:
                            lines.append(f"        if ({valid_fn})")
                            lines.append(f"            {port.name} <= {result};")
                lines.append("    end")
                lines.append("")

        lines.append("endmodule")

        output_path = self.bridges_dir / f"{module_name}.sv"
        output_path.write_text("\n".join(lines))
        logger.info(f"Generated static-side DPI bridge: {output_path}")
        return output_path

    def generate_rm_side_wrapper(
        self,
        boundary: PartitionBoundaryDef,
        rm_design_name: str,
    ) -> Path:
        """
        Generate the RM-side DPI wrapper.

        This module wraps the real RM, connecting its ports to DPI channels.
        Input ports are driven by data received from the static region via DPI.
        Output ports are sent to the static region via DPI.

        Internal signals use `/* verilator public */` annotation for direct
        C++ access (Python signal reads/writes).
        """
        part = boundary.partition_name
        to_rm = [p for p in boundary.ports if p.direction == 'to_rm']
        from_rm = [p for p in boundary.ports if p.direction == 'from_rm']
        wrapper_name = f"{rm_design_name}_dpi_wrapper"

        lines = []
        lines.append(f"// RM-side DPI wrapper for {rm_design_name}")
        lines.append(f"// Partition: {part}")
        lines.append("// Generated by partial_reconfiguration DPI bridge generator")
        lines.append("")
        lines.append("`timescale 1ns/1ps")
        lines.append("")

        # Module declaration — all clocks + optional reset as ports
        lines.append(f"module {wrapper_name} (")
        wrapper_ports = [f"    input wire {clk}" for clk in boundary.clock_names]
        if boundary.reset_name is not None:
            wrapper_ports.append(f"    input wire {boundary.reset_name}")
        lines.append(",\n".join(wrapper_ports))
        lines.append(");")
        lines.append("")

        # DPI imports — width-matched types, chunked for wide ports
        for port in to_rm:
            nc = port.num_chunks
            for c in range(nc):
                cw = _chunk_width(port.width, c)
                dt = _dpi_type(cw)
                suffix_d = f"_chunk{c}_recv_data" if nc > 1 else "_recv_data"
                suffix_v = f"_chunk{c}_recv_valid" if nc > 1 else "_recv_valid"
                lines.append(
                    f'    import "DPI-C" function {dt} '
                    f'dpi_rm_{part}_{port.name}{suffix_d}();'
                )
                lines.append(
                    f'    import "DPI-C" function int '
                    f'dpi_rm_{part}_{port.name}{suffix_v}();'
                )

        for port in from_rm:
            nc = port.num_chunks
            for c in range(nc):
                cw = _chunk_width(port.width, c)
                dt = _dpi_type(cw)
                suffix = f"_chunk{c}_send" if nc > 1 else "_send"
                lines.append(
                    f'    import "DPI-C" function void '
                    f'dpi_rm_{part}_{port.name}{suffix}(input {dt} data);'
                )
        lines.append("")

        # Internal registers for to_rm ports (inputs to RM)
        for port in to_rm:
            width_str = f"[{port.width-1}:0]" if port.width > 1 else ""
            lines.append(f"    reg {width_str} {port.name} /* verilator public */;")

        # Internal wires for from_rm ports (outputs from RM)
        for port in from_rm:
            width_str = f"[{port.width-1}:0]" if port.width > 1 else ""
            lines.append(f"    wire {width_str} {port.name} /* verilator public */;")
        lines.append("")

        # Instantiate the real RM
        lines.append(f"    // Instantiate the real RM: {rm_design_name}")
        lines.append(f"    {rm_design_name} u_rm (")
        # Connect all clocks
        rm_conns = []
        for clk in boundary.clock_names:
            rm_conns.append(f"        .{clk}({clk})")
        # Connect reset if present
        if boundary.reset_name is not None:
            rm_conns.append(f"        .{boundary.reset_name}({boundary.reset_name})")
        for port in to_rm:
            rm_conns.append(f"        .{port.name}({port.name})")
        for port in from_rm:
            rm_conns.append(f"        .{port.name}({port.name})")
        lines.append(",\n".join(rm_conns))
        lines.append("    );")
        lines.append("")

        # Group ports by their effective clock
        def _port_clock(p):
            return p.clock or boundary.clock_names[0]

        # Update inputs from DPI channels — grouped per clock
        if to_rm:
            clk_groups = {}
            for port in to_rm:
                clk = _port_clock(port)
                clk_groups.setdefault(clk, []).append(port)
            for clk, ports in clk_groups.items():
                lines.append(f"    // Update inputs from DPI channels (clock: {clk})")
                lines.append(f"    always @(posedge {clk}) begin")
                for port in ports:
                    nc = port.num_chunks
                    for c in range(nc):
                        suffix_d = f"_chunk{c}_recv_data" if nc > 1 else "_recv_data"
                        suffix_v = f"_chunk{c}_recv_valid" if nc > 1 else "_recv_valid"
                        recv_fn = f"dpi_rm_{part}_{port.name}{suffix_d}()"
                        result = _sv_recv_trunc(port, recv_fn, c)
                        valid_fn = f"dpi_rm_{part}_{port.name}{suffix_v}()"
                        if isinstance(result, tuple):
                            lhs, rhs = result
                            lines.append(f"        if ({valid_fn})")
                            lines.append(f"            {lhs} <= {rhs};")
                        else:
                            lines.append(f"        if ({valid_fn})")
                            lines.append(f"            {port.name} <= {result};")
                lines.append("    end")
                lines.append("")

        # Send outputs to static via DPI — grouped per clock
        if from_rm:
            clk_groups = {}
            for port in from_rm:
                clk = _port_clock(port)
                clk_groups.setdefault(clk, []).append(port)
            for clk, ports in clk_groups.items():
                lines.append(f"    // Send outputs to static region via DPI channels (clock: {clk})")
                lines.append(f"    always @(posedge {clk}) begin")
                for port in ports:
                    nc = port.num_chunks
                    for c in range(nc):
                        arg = _sv_send_cast(port, c)
                        suffix = f"_chunk{c}_send" if nc > 1 else "_send"
                        lines.append(f"        dpi_rm_{part}_{port.name}{suffix}({arg});")
                lines.append("    end")
                lines.append("")

        lines.append("endmodule")

        output_path = self.wrappers_dir / f"{wrapper_name}.sv"
        output_path.write_text("\n".join(lines))
        logger.info(f"Generated RM-side DPI wrapper: {output_path}")
        return output_path

    def generate_both(
        self,
        boundary: PartitionBoundaryDef,
        rm_design_name: str,
    ) -> tuple:
        """Generate both static-side bridge and RM-side wrapper."""
        bridge_path = self.generate_static_side_bridge(boundary)
        wrapper_path = self.generate_rm_side_wrapper(boundary, rm_design_name)
        return bridge_path, wrapper_path
