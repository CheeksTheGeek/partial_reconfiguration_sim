"""
DPI C++ Code Generator for multi-binary simulation.

Generates C++ code for the multi-process simulation architecture:
- dpi_shm_channel.h: Shared memory channel layout structs
- barrier_sync.h: Sense-reversing atomic barrier for cross-process sync
- shm_mailbox.h: Python<->C++ shared memory command mailbox struct
- signal_access.h: Generated port->signal accessor functions
- static_driver.cpp: Static binary main driver (single-threaded)
- rm_driver_{variant}.cpp: Per-RM-variant driver (single-threaded)
- dpi_static_{partition}.cpp: Static-side DPI function implementations
- dpi_rm_{partition}.cpp: RM-side DPI function implementations
"""
from typing import List, Dict, Any, Optional
from pathlib import Path
from dataclasses import dataclass, field
import logging

logger = logging.getLogger(__name__)


@dataclass
class PartitionInfo:
    """Info about a partition for code generation."""
    name: str
    index: int  # 0-based partition index
    rm_module_name: str  # Default RM module name (for bridge)
    clock_names: List[str]  # All clocks for this partition
    to_rm_ports: List[Dict[str, Any]]  # [{name, width}, ...]
    from_rm_ports: List[Dict[str, Any]]
    rm_variants: List[Dict[str, Any]] = field(default_factory=list)
    # [{name, design, wrapper_name, index}, ...]
    initial_rm_index: int = 0
    reset_name: Optional[str] = None
    reset_polarity: str = 'negative'
    reset_cycles: int = 10
    reset_behavior: str = 'fresh'  # 'fresh', 'gsr_xilinx', 'none_intel'

    @property
    def clock_name(self) -> str:
        """Backward-compat: primary clock."""
        return self.clock_names[0]


@dataclass
class StaticInfo:
    """Info about the static region for code generation."""
    design_name: str
    ports: List[Dict[str, Any]]  # [{name, width, direction}, ...]
    # direction: 'input' or 'output'
    clock_name: str = 'clk'


def _num_chunks(width: int) -> int:
    """Number of 64-bit SHM slots needed for a port of the given width."""
    return (width + 63) // 64


def _chunk_width(port_width: int, chunk_idx: int) -> int:
    """Return the effective bit width of a specific chunk."""
    lo = chunk_idx * 64
    hi = min(lo + 63, port_width - 1)
    return hi - lo + 1


def _slot_offsets(ports: List[Dict]) -> List[int]:
    """Return the starting slot index for each port in the list."""
    offsets, idx = [], 0
    for p in ports:
        offsets.append(idx)
        idx += _num_chunks(p.get('width', 32))
    return offsets


def _total_slots(ports: List[Dict]) -> int:
    """Total number of SHM slots for a port list."""
    return sum(_num_chunks(p.get('width', 32)) for p in ports)


def _cpp_type(width: int) -> str:
    """Select the C++ type that matches the DPI-C type for a given bit width (per-chunk)."""
    if width <= 32:
        return 'int'
    else:
        return 'long long'


def _cpp_unsigned_type(width: int) -> str:
    """Select the unsigned C++ storage type for a given bit width (per-chunk)."""
    if width <= 32:
        return 'uint32_t'
    else:
        return 'uint64_t'


def _mask_expr(width: int) -> str:
    """Generate a C++ mask expression for the given bit width, or empty if full-width."""
    if width == 32:
        return ''
    if width == 64:
        return ''
    if width > 64:
        return ''  # callers use per-chunk widths; this shouldn't be called for >64
    if width < 32:
        mask = (1 << width) - 1
        return f" & 0x{mask:X}u"
    # 33-63 bits
    mask = (1 << width) - 1
    return f" & 0x{mask:X}ull"


class DpiCppGenerator:
    """Generates all C++ source files for the multi-binary DPI simulation."""

    def __init__(self, build_dir: str):
        self.build_dir = Path(build_dir)
        self.dpi_dir = self.build_dir / 'dpi'
        self.dpi_dir.mkdir(parents=True, exist_ok=True)

    def generate_all(
        self,
        partitions: List[PartitionInfo],
        static_info: StaticInfo,
        trace: bool = False,
        trace_type: str = 'vcd',
    ):
        """Generate all C++ files for the multi-binary architecture."""
        self.generate_dpi_shm_channel_h(partitions)
        self.generate_barrier_sync_h()
        self.generate_shm_mailbox_h()
        self.generate_signal_access_h(partitions, static_info)
        self.generate_static_driver_cpp(partitions, static_info, trace, trace_type)
        for part in partitions:
            self.generate_dpi_static_partition_cpp(part)
            self.generate_dpi_rm_partition_cpp(part)
        for part in partitions:
            for rm in part.rm_variants:
                self.generate_rm_driver_cpp(part, rm, trace, trace_type)

    # ── dpi_shm_channel.h ──────────────────────────────────────────────

    def generate_dpi_shm_channel_h(self, partitions: List[PartitionInfo]):
        """Generate shared memory channel layout structs."""
        lines = []
        lines.append("#ifndef DPI_SHM_CHANNEL_H")
        lines.append("#define DPI_SHM_CHANNEL_H")
        lines.append("")
        lines.append("#include <cstdint>")
        lines.append("#include <cstddef>")
        lines.append("")
        lines.append("#define SHM_MAGIC   0x50525348  // \"PRSH\"")
        lines.append("#define SHM_VERSION 1")
        lines.append("")

        # ShmPort: one cache line per port slot
        lines.append("// Port data slot (one cache line = 64 bytes)")
        lines.append("struct alignas(64) ShmPort {")
        lines.append("    uint64_t data;")
        lines.append("    uint32_t valid;")
        lines.append("};")
        lines.append("static_assert(sizeof(ShmPort) == 64);")
        lines.append("")

        # ShmPortOverride: for Python-side writes
        lines.append("// Port override slot for Python writes (one cache line)")
        lines.append("struct alignas(64) ShmPortOverride {")
        lines.append("    uint64_t value;")
        lines.append("    uint32_t active;")
        lines.append("};")
        lines.append("static_assert(sizeof(ShmPortOverride) == 64);")
        lines.append("")

        # ShmPartitionHeader: first cache line of each partition file
        lines.append("// Partition shared memory header (one cache line)")
        lines.append("struct alignas(64) ShmPartitionHeader {")
        lines.append("    uint32_t magic;          // SHM_MAGIC")
        lines.append("    uint32_t version;        // SHM_VERSION")
        lines.append("    uint32_t num_to_rm;")
        lines.append("    uint32_t num_from_rm;")
        lines.append("    uint32_t initialized;    // Set by creator")
        lines.append("    uint32_t quit;            // Static sets to signal RM exit")
        lines.append("    uint32_t rm_ready;        // New RM sets when ready")
        lines.append("};")
        lines.append("static_assert(sizeof(ShmPartitionHeader) == 64);")
        lines.append("")

        # Layout helpers
        lines.append("// Memory layout per partition shm file:")
        lines.append("//   [0]              ShmPartitionHeader    (64 bytes)")
        lines.append("//   [64]             to_rm[0].outbox       (64 bytes)")
        lines.append("//   [128]            to_rm[0].inbox        (64 bytes)")
        lines.append("//   [192]            to_rm[0].override     (64 bytes)")
        lines.append("//   [256]            to_rm[1].outbox       ...")
        lines.append("//   ...              (T ports * 192 bytes)")
        lines.append("//   [64 + T*192]     from_rm[0].outbox     (64 bytes)")
        lines.append("//   [64 + T*192+64]  from_rm[0].inbox      (64 bytes)")
        lines.append("//   ...              (F ports * 128 bytes)")
        lines.append("//   Total: 64 + T*192 + F*128 bytes, page-aligned")
        lines.append("")

        lines.append("// T_slots and F_slots are slot counts (not port counts).")
        lines.append("// A port of width W occupies ceil(W/64) slots.")
        lines.append("static inline size_t shm_partition_size(uint32_t T_slots, uint32_t F_slots) {")
        lines.append("    size_t raw = 64 + (size_t)T_slots * 192 + (size_t)F_slots * 128;")
        lines.append("    return (raw + 4095) & ~(size_t)4095;  // round to page")
        lines.append("}")
        lines.append("")

        # Accessor helpers
        lines.append("// Accessors — all return pointers into the mmap'd region")
        lines.append("static inline ShmPartitionHeader* shm_header(void* base) {")
        lines.append("    return (ShmPartitionHeader*)base;")
        lines.append("}")
        lines.append("")
        lines.append("static inline ShmPort* shm_to_rm_outbox(void* base, int idx) {")
        lines.append("    return (ShmPort*)((char*)base + 64 + idx * 192);")
        lines.append("}")
        lines.append("static inline ShmPort* shm_to_rm_inbox(void* base, int idx) {")
        lines.append("    return (ShmPort*)((char*)base + 64 + idx * 192 + 64);")
        lines.append("}")
        lines.append("static inline ShmPortOverride* shm_to_rm_override(void* base, int idx) {")
        lines.append("    return (ShmPortOverride*)((char*)base + 64 + idx * 192 + 128);")
        lines.append("}")
        lines.append("")
        lines.append("static inline ShmPort* shm_from_rm_outbox(void* base, uint32_t T, int idx) {")
        lines.append("    return (ShmPort*)((char*)base + 64 + T * 192 + idx * 128);")
        lines.append("}")
        lines.append("static inline ShmPort* shm_from_rm_inbox(void* base, uint32_t T, int idx) {")
        lines.append("    return (ShmPort*)((char*)base + 64 + T * 192 + idx * 128 + 64);")
        lines.append("}")
        lines.append("")

        # Atomic helpers (GCC/Clang builtins for mmap'd memory)
        lines.append("// Atomic helpers for cross-process shared memory")
        lines.append("static inline uint32_t shm_load32(volatile uint32_t* p) {")
        lines.append("    return __atomic_load_n(p, __ATOMIC_ACQUIRE);")
        lines.append("}")
        lines.append("static inline void shm_store32(volatile uint32_t* p, uint32_t v) {")
        lines.append("    __atomic_store_n(p, v, __ATOMIC_RELEASE);")
        lines.append("}")
        lines.append("static inline uint32_t shm_load32_relaxed(volatile uint32_t* p) {")
        lines.append("    return __atomic_load_n(p, __ATOMIC_RELAXED);")
        lines.append("}")
        lines.append("static inline void shm_store32_relaxed(volatile uint32_t* p, uint32_t v) {")
        lines.append("    __atomic_store_n(p, v, __ATOMIC_RELAXED);")
        lines.append("}")
        lines.append("")
        lines.append("static inline uint64_t shm_load64_relaxed(volatile uint64_t* p) {")
        lines.append("    return __atomic_load_n(p, __ATOMIC_RELAXED);")
        lines.append("}")
        lines.append("static inline void shm_store64_relaxed(volatile uint64_t* p, uint64_t v) {")
        lines.append("    __atomic_store_n(p, v, __ATOMIC_RELAXED);")
        lines.append("}")
        lines.append("")

        lines.append("#endif // DPI_SHM_CHANNEL_H")

        path = self.dpi_dir / "dpi_shm_channel.h"
        path.write_text("\n".join(lines))
        logger.info(f"Generated: {path}")

    # ── barrier_sync.h ─────────────────────────────────────────────────

    def generate_barrier_sync_h(self):
        """Generate sense-reversing atomic barrier for cross-process sync."""
        lines = []
        lines.append("#ifndef BARRIER_SYNC_H")
        lines.append("#define BARRIER_SYNC_H")
        lines.append("")
        lines.append("#include <cstdint>")
        lines.append("")
        lines.append("// Shared memory barrier layout (matches Python CycleBarrier)")
        lines.append("// Each field on its own cache line to avoid false sharing.")
        lines.append("struct ShmBarrier {")
        lines.append("    alignas(64) volatile uint64_t cycle_count;   // offset 0")
        lines.append("    alignas(64) volatile uint32_t count;         // offset 64")
        lines.append("    alignas(64) volatile uint32_t num_processes; // offset 128")
        lines.append("    alignas(64) volatile uint32_t sense;         // offset 192")
        lines.append("    alignas(64) volatile uint32_t initialized;   // offset 256")
        lines.append("};")
        lines.append("")
        lines.append("// Sense-reversing barrier wait.")
        lines.append("// Each process keeps a local_sense variable (init from barrier->sense).")
        lines.append("static inline void barrier_wait(ShmBarrier* b, uint32_t* local_sense) {")
        lines.append("    *local_sense = 1 - *local_sense;")
        lines.append("    uint32_t arrived = __atomic_add_fetch(&b->count, 1, __ATOMIC_ACQ_REL);")
        lines.append("    if (arrived == __atomic_load_n(&b->num_processes, __ATOMIC_ACQUIRE)) {")
        lines.append("        __atomic_store_n(&b->count, 0, __ATOMIC_RELAXED);")
        lines.append("        __atomic_store_n(&b->sense, *local_sense, __ATOMIC_RELEASE);")
        lines.append("    } else {")
        lines.append("        while (__atomic_load_n(&b->sense, __ATOMIC_ACQUIRE) != *local_sense) {")
        lines.append("            // spin")
        lines.append("        }")
        lines.append("    }")
        lines.append("}")
        lines.append("")
        lines.append("#endif // BARRIER_SYNC_H")

        path = self.dpi_dir / "barrier_sync.h"
        path.write_text("\n".join(lines))
        logger.info(f"Generated: {path}")

    # ── shm_mailbox.h ──────────────────────────────────────────────────

    def generate_shm_mailbox_h(self):
        """Generate shared memory mailbox struct for Python<->static communication."""
        lines = []
        lines.append("#ifndef SHM_MAILBOX_H")
        lines.append("#define SHM_MAILBOX_H")
        lines.append("")
        lines.append("#include <cstdint>")
        lines.append("")
        lines.append("#define SHM_MAILBOX_SIZE 4096")
        lines.append("")
        lines.append("// Command codes")
        lines.append("#define CMD_NOOP     0")
        lines.append("#define CMD_READ     1")
        lines.append("#define CMD_WRITE    2")
        lines.append("#define CMD_RECONFIG 3")
        lines.append("#define CMD_BATCH    4")
        lines.append("#define CMD_QUIT     0xFF")
        lines.append("")
        lines.append("// Batch command limits")
        lines.append("#define MAX_BATCH    32")
        lines.append("")
        lines.append("// Simulation status")
        lines.append("#define SIM_STATUS_INIT    0")
        lines.append("#define SIM_STATUS_RUNNING 1")
        lines.append("#define SIM_STATUS_DONE    2")
        lines.append("#define SIM_STATUS_ERROR   3")
        lines.append("")
        lines.append("// Target codes for read/write")
        lines.append("#define TARGET_STATIC 0")
        lines.append("// Partition targets: 1, 2, 3, ... (1-based)")
        lines.append("")
        lines.append("struct ShmBatchSlot {")
        lines.append("    uint32_t cmd;          // CMD_READ or CMD_WRITE")
        lines.append("    uint32_t target;")
        lines.append("    uint32_t port_idx;")
        lines.append("    uint32_t _pad;")
        lines.append("    uint64_t write_value;")
        lines.append("    uint64_t read_value;   // written by C++ for CMD_READ")
        lines.append("};")
        lines.append("")
        lines.append("struct ShmMailbox {")
        lines.append("    volatile uint32_t sim_status;     // SIM_STATUS_*")
        lines.append("    uint32_t _pad0;")
        lines.append("    volatile uint64_t cycle_count;    // Current cycle count")
        lines.append("    volatile uint32_t cmd;            // CMD_*")
        lines.append("    volatile uint32_t target;         // TARGET_STATIC or partition index (1-based)")
        lines.append("    volatile uint32_t port_idx;       // Port index within target")
        lines.append("    volatile uint32_t rm_idx;         // RM index for reconfig")
        lines.append("    volatile uint64_t write_value;    // Value for write commands")
        lines.append("    volatile uint64_t read_value;     // Value from read commands")
        lines.append("    // Batch command extension (offset 48)")
        lines.append("    volatile uint32_t batch_count;    // Number of batch slots used")
        lines.append("    uint32_t _pad1;")
        lines.append("    ShmBatchSlot batch[MAX_BATCH];    // 32 slots x 32 bytes = 1024 bytes")
        lines.append("};")
        lines.append("")
        lines.append("#endif // SHM_MAILBOX_H")

        path = self.dpi_dir / "shm_mailbox.h"
        path.write_text("\n".join(lines))
        logger.info(f"Generated: {path}")

    # ── signal_access.h ────────────────────────────────────────────────

    def generate_signal_access_h(
        self,
        partitions: List[PartitionInfo],
        static_info: StaticInfo,
    ):
        """Generate signal accessor functions for Python read/write via static binary."""
        lines = []
        lines.append("#ifndef SIGNAL_ACCESS_H")
        lines.append("#define SIGNAL_ACCESS_H")
        lines.append("")
        lines.append("#include <cstdint>")
        lines.append("#include <verilated.h>")
        lines.append('#include "dpi_shm_channel.h"')
        lines.append("")

        # Include Verilator-generated header for static model
        static_model_type = f"V{static_info.design_name}"
        lines.append(f'#include "{static_model_type}.h"')
        lines.append("")
        lines.append("// Global partition bases (defined in static_driver.cpp)")
        lines.append("extern void* g_partition_bases[];")
        lines.append("")

        # Static region read
        lines.append(f"inline uint64_t read_static_port({static_model_type}* model, int port_idx) {{")
        lines.append("    switch (port_idx) {")
        for i, port in enumerate(static_info.ports):
            lines.append(f"        case {i}: return static_cast<uint64_t>(model->{port['name']});")
        lines.append("        default: return 0;")
        lines.append("    }")
        lines.append("}")
        lines.append("")

        # Static region write (for input ports)
        lines.append(f"inline void write_static_port({static_model_type}* model, int port_idx, uint64_t value) {{")
        lines.append("    switch (port_idx) {")
        for i, port in enumerate(static_info.ports):
            if port['direction'] == 'input':
                w = port.get('width', 32)
                cast = _cpp_unsigned_type(w)
                lines.append(f"        case {i}: model->{port['name']} = static_cast<{cast}>(value); break;")
        lines.append("        default: break;")
        lines.append("    }")
        lines.append("}")
        lines.append("")

        # Per-partition read/write via shared memory (slot-based indexing)
        for part in partitions:
            to_offsets = _slot_offsets(part.to_rm_ports)
            num_to_rm_slots = _total_slots(part.to_rm_ports)

            lines.append(f"inline uint64_t read_{part.name}_port(int port_idx) {{")
            lines.append(f"    void* base = g_partition_bases[{part.index}];")
            lines.append(f"    ShmPartitionHeader* hdr = shm_header(base);")
            lines.append("    switch (port_idx) {")
            # to_rm slots: each slot has override + inbox
            slot_idx = 0
            for i, port in enumerate(part.to_rm_ports):
                nc = _num_chunks(port.get('width', 32))
                for c in range(nc):
                    lines.append(f"        case {slot_idx}: {{")
                    lines.append(f"            ShmPortOverride* ovr = shm_to_rm_override(base, {slot_idx});")
                    lines.append(f"            if (shm_load32(&ovr->active)) return shm_load64_relaxed(&ovr->value);")
                    lines.append(f"            return shm_load64_relaxed(&shm_to_rm_inbox(base, {slot_idx})->data);")
                    lines.append(f"        }}")
                    slot_idx += 1
            # from_rm slots: read from outbox
            from_slot_idx = 0
            for i, port in enumerate(part.from_rm_ports):
                nc = _num_chunks(port.get('width', 32))
                for c in range(nc):
                    lines.append(f"        case {slot_idx}: return shm_load64_relaxed(&shm_from_rm_outbox(base, hdr->num_to_rm, {from_slot_idx})->data);")
                    slot_idx += 1
                    from_slot_idx += 1
            lines.append("        default: return 0;")
            lines.append("    }")
            lines.append("}")
            lines.append("")

            lines.append(f"inline void write_{part.name}_port(int port_idx, uint64_t value) {{")
            lines.append(f"    void* base = g_partition_bases[{part.index}];")
            lines.append("    switch (port_idx) {")
            slot_idx = 0
            for i, port in enumerate(part.to_rm_ports):
                nc = _num_chunks(port.get('width', 32))
                for c in range(nc):
                    lines.append(f"        case {slot_idx}: {{")
                    lines.append(f"            ShmPortOverride* ovr = shm_to_rm_override(base, {slot_idx});")
                    lines.append(f"            shm_store64_relaxed(&ovr->value, value);")
                    lines.append(f"            shm_store32(&ovr->active, 1);")
                    lines.append(f"            break;")
                    lines.append(f"        }}")
                    slot_idx += 1
            # from_rm ports are read-only from Python
            lines.append("        default: break;")
            lines.append("    }")
            lines.append("}")
            lines.append("")

        lines.append("#endif // SIGNAL_ACCESS_H")

        path = self.dpi_dir / "signal_access.h"
        path.write_text("\n".join(lines))
        logger.info(f"Generated: {path}")

    # ── static_driver.cpp ──────────────────────────────────────────────

    def generate_static_driver_cpp(
        self,
        partitions: List[PartitionInfo],
        static_info: StaticInfo,
        trace: bool = False,
        trace_type: str = 'vcd',
    ):
        """Generate the single-threaded static binary driver."""
        static_model_type = f"V{static_info.design_name}"
        num_parts = len(partitions)
        lines = []
        lines.append("// Static binary driver — generated by partial_reconfiguration")
        lines.append("// Single-threaded, multi-process architecture")
        lines.append("")
        lines.append('#include "dpi_shm_channel.h"')
        lines.append('#include "barrier_sync.h"')
        lines.append('#include "shm_mailbox.h"')
        lines.append("")
        lines.append("#include <verilated.h>")
        if trace:
            if trace_type == 'fst':
                lines.append("#include <verilated_fst_c.h>")
            else:
                lines.append("#include <verilated_vcd_c.h>")
        lines.append(f'#include "V{static_info.design_name}.h"')
        lines.append('#include "signal_access.h"')
        lines.append("")
        lines.append("#include <cstring>")
        lines.append("#include <iostream>")
        lines.append("#include <sys/mman.h>")
        lines.append("#include <sys/stat.h>")
        lines.append("#include <fcntl.h>")
        lines.append("#include <unistd.h>")
        lines.append("")
        lines.append("// Required by Verilator")
        lines.append("double sc_time_stamp() { return 0; }")
        lines.append("")

        # Globals
        lines.append(f"#define NUM_PARTITIONS {num_parts}")
        lines.append("")
        lines.append(f"static {static_model_type}* static_model = nullptr;")
        lines.append(f"static VerilatedContext* static_ctx = nullptr;")
        lines.append("static ShmMailbox* mailbox = nullptr;")
        lines.append("static ShmBarrier* barrier = nullptr;")
        lines.append("")
        lines.append("// Partition channel bases (exported for signal_access.h)")
        lines.append("void* g_partition_bases[NUM_PARTITIONS];")
        lines.append("static ShmPartitionHeader* g_partition_headers[NUM_PARTITIONS];")
        lines.append("static size_t g_partition_sizes[NUM_PARTITIONS];")
        lines.append("")

        # swap_channels
        lines.append("static void swap_channels() {")
        lines.append("    for (int p = 0; p < NUM_PARTITIONS; p++) {")
        lines.append("        void* base = g_partition_bases[p];")
        lines.append("        ShmPartitionHeader* hdr = g_partition_headers[p];")
        lines.append("        uint32_t T = hdr->num_to_rm;")
        lines.append("        uint32_t F = hdr->num_from_rm;")
        lines.append("        for (uint32_t i = 0; i < T; i++) {")
        lines.append("            ShmPort* ob = shm_to_rm_outbox(base, i);")
        lines.append("            ShmPort* ib = shm_to_rm_inbox(base, i);")
        lines.append("            ib->data = ob->data;")
        lines.append("            ib->valid = ob->valid;")
        lines.append("            ob->valid = 0;")
        lines.append("        }")
        lines.append("        for (uint32_t i = 0; i < F; i++) {")
        lines.append("            ShmPort* ob = shm_from_rm_outbox(base, T, i);")
        lines.append("            ShmPort* ib = shm_from_rm_inbox(base, T, i);")
        lines.append("            ib->data = ob->data;")
        lines.append("            ib->valid = ob->valid;")
        lines.append("            ob->valid = 0;")
        lines.append("        }")
        lines.append("    }")
        lines.append("}")
        lines.append("")

        # process_commands
        lines.append("static int reconfig_partition = -1;")
        lines.append("")
        lines.append("static void process_commands() {")
        lines.append("    if (mailbox->cmd == CMD_NOOP) return;")
        lines.append("")
        lines.append("    switch (mailbox->cmd) {")
        lines.append("    case CMD_READ: {")
        lines.append("        uint64_t val = 0;")
        lines.append("        if (mailbox->target == TARGET_STATIC) {")
        lines.append("            val = read_static_port(static_model, mailbox->port_idx);")
        lines.append("        }")
        for part in partitions:
            lines.append(f"        else if (mailbox->target == {part.index + 1}) {{")
            lines.append(f"            val = read_{part.name}_port(mailbox->port_idx);")
            lines.append(f"        }}")
        lines.append("        mailbox->read_value = val;")
        lines.append("        mailbox->cmd = CMD_NOOP;")
        lines.append("        break;")
        lines.append("    }")
        lines.append("    case CMD_WRITE: {")
        lines.append("        if (mailbox->target == TARGET_STATIC) {")
        lines.append("            write_static_port(static_model, mailbox->port_idx, mailbox->write_value);")
        lines.append("        }")
        for part in partitions:
            lines.append(f"        else if (mailbox->target == {part.index + 1}) {{")
            lines.append(f"            write_{part.name}_port(mailbox->port_idx, mailbox->write_value);")
            lines.append(f"        }}")
        lines.append("        mailbox->cmd = CMD_NOOP;")
        lines.append("        break;")
        lines.append("    }")
        lines.append("    case CMD_RECONFIG: {")
        lines.append("        int part_id = mailbox->target - 1;")
        lines.append("        if (part_id >= 0 && part_id < NUM_PARTITIONS) {")
        lines.append("            // Set quit flag for target partition — RM will exit after current cycle")
        lines.append("            shm_store32(&g_partition_headers[part_id]->quit, 1);")
        # Clear overrides on reconfig
        lines.append("            // Clear overrides")
        lines.append("            void* base = g_partition_bases[part_id];")
        lines.append("            for (uint32_t i = 0; i < g_partition_headers[part_id]->num_to_rm; i++) {")
        lines.append("                ShmPortOverride* ovr = shm_to_rm_override(base, i);")
        lines.append("                shm_store32(&ovr->active, 0);")
        lines.append("            }")
        lines.append("            reconfig_partition = part_id;")
        lines.append("        }")
        lines.append("        // Don't set cmd=NOOP yet — Python waits until reconfig completes")
        lines.append("        break;")
        lines.append("    }")
        lines.append("    case CMD_BATCH: {")
        lines.append("        uint32_t n = mailbox->batch_count;")
        lines.append("        if (n > MAX_BATCH) n = MAX_BATCH;")
        lines.append("        for (uint32_t b = 0; b < n; b++) {")
        lines.append("            ShmBatchSlot* slot = &mailbox->batch[b];")
        lines.append("            switch (slot->cmd) {")
        lines.append("            case CMD_READ: {")
        lines.append("                uint64_t val = 0;")
        lines.append("                if (slot->target == TARGET_STATIC) {")
        lines.append("                    val = read_static_port(static_model, slot->port_idx);")
        lines.append("                }")
        for part in partitions:
            lines.append(f"                else if (slot->target == {part.index + 1}) {{")
            lines.append(f"                    val = read_{part.name}_port(slot->port_idx);")
            lines.append(f"                }}")
        lines.append("                slot->read_value = val;")
        lines.append("                break;")
        lines.append("            }")
        lines.append("            case CMD_WRITE: {")
        lines.append("                if (slot->target == TARGET_STATIC) {")
        lines.append("                    write_static_port(static_model, slot->port_idx, slot->write_value);")
        lines.append("                }")
        for part in partitions:
            lines.append(f"                else if (slot->target == {part.index + 1}) {{")
            lines.append(f"                    write_{part.name}_port(slot->port_idx, slot->write_value);")
            lines.append(f"                }}")
        lines.append("                break;")
        lines.append("            }")
        lines.append("            }")
        lines.append("        }")
        lines.append("        mailbox->cmd = CMD_NOOP;")
        lines.append("        break;")
        lines.append("    }")
        lines.append("    case CMD_QUIT:")
        lines.append("        mailbox->cmd = CMD_NOOP;")
        lines.append("        break;")
        lines.append("    }")
        lines.append("}")
        lines.append("")

        # mmap helper
        lines.append("static void* mmap_file(const char* path, size_t expected_size, int* fd_out) {")
        lines.append("    int fd = open(path, O_RDWR);")
        lines.append("    if (fd < 0) { perror(path); return nullptr; }")
        lines.append("    struct stat st;")
        lines.append("    fstat(fd, &st);")
        lines.append("    size_t sz = (expected_size > 0) ? expected_size : (size_t)st.st_size;")
        lines.append("    void* ptr = mmap(nullptr, sz, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);")
        lines.append("    if (ptr == MAP_FAILED) { perror(\"mmap\"); close(fd); return nullptr; }")
        lines.append("    if (fd_out) *fd_out = fd; else close(fd);")
        lines.append("    return ptr;")
        lines.append("}")
        lines.append("")

        # main
        lines.append("int main(int argc, char** argv) {")
        lines.append('    const char* shm_dir = nullptr;')
        lines.append("    for (int i = 1; i < argc; i++) {")
        lines.append('        if (std::string(argv[i]) == "--shm-dir" && i + 1 < argc)')
        lines.append('            shm_dir = argv[++i];')
        lines.append("    }")
        lines.append('    if (!shm_dir) { std::cerr << "Error: --shm-dir required" << std::endl; return 1; }')
        lines.append("")
        lines.append("    std::string dir(shm_dir);")
        lines.append("")

        # Open mailbox
        lines.append("    // Open Python mailbox")
        lines.append('    int mbox_fd = -1;')
        lines.append('    mailbox = (ShmMailbox*)mmap_file((dir + "/cmd_mailbox.shm").c_str(), SHM_MAILBOX_SIZE, &mbox_fd);')
        lines.append("    if (!mailbox) return 1;")
        lines.append("")

        # Open barrier
        lines.append("    // Open barrier")
        lines.append("    int barrier_fd = -1;")
        lines.append('    barrier = (ShmBarrier*)mmap_file((dir + "/barrier.shm").c_str(), 4096, &barrier_fd);')
        lines.append("    if (!barrier) return 1;")
        lines.append("")

        # Open partition channels
        lines.append("    // Open partition channels")
        lines.append("    int ch_fds[NUM_PARTITIONS];")
        lines.append("    for (int p = 0; p < NUM_PARTITIONS; p++) {")
        lines.append('        std::string path = dir + "/partition_" + std::to_string(p) + ".shm";')
        lines.append("        g_partition_bases[p] = mmap_file(path.c_str(), 0, &ch_fds[p]);")
        lines.append("        if (!g_partition_bases[p]) return 1;")
        lines.append("        g_partition_headers[p] = shm_header(g_partition_bases[p]);")
        lines.append("        struct stat st; fstat(ch_fds[p], &st);")
        lines.append("        g_partition_sizes[p] = st.st_size;")
        lines.append("    }")
        lines.append("")

        # Create static model
        lines.append("    // Create static region model")
        lines.append("    static_ctx = new VerilatedContext;")
        lines.append(f"    static_model = new {static_model_type}(static_ctx);")
        lines.append("")

        # Signal ready
        lines.append("    mailbox->sim_status = SIM_STATUS_RUNNING;")
        lines.append("    mailbox->cycle_count = 0;")
        lines.append("")

        # Init local sense from barrier
        lines.append("    // Initialize local barrier sense")
        lines.append("    uint32_t local_sense = __atomic_load_n(&barrier->sense, __ATOMIC_ACQUIRE);")
        lines.append("")

        # Main loop
        lines.append("    // Main simulation loop")
        lines.append("    uint64_t cycle = 0;")
        lines.append("    bool quit_global = false;")
        lines.append("")
        lines.append("    while (!quit_global) {")
        clk = static_info.clock_name
        lines.append("        // Phase 1: negedge eval")
        lines.append(f"        static_model->{clk} = 0;")
        lines.append("        static_model->eval();")
        lines.append("        barrier_wait(barrier, &local_sense);  // 1: negedge done")
        lines.append("")
        lines.append("        // Phase 2: swap channels + process commands")
        lines.append("        swap_channels();")
        lines.append("        process_commands();")
        lines.append("        if (mailbox->cmd == CMD_QUIT) { quit_global = true; }")
        lines.append("        barrier_wait(barrier, &local_sense);  // 2: swap done")
        lines.append("")
        lines.append("        // Phase 3: posedge eval")
        lines.append(f"        static_model->{clk} = 1;")
        lines.append("        static_model->eval();")
        lines.append("        barrier_wait(barrier, &local_sense);  // 3: posedge done")
        lines.append("")
        lines.append("        cycle++;")
        lines.append("        mailbox->cycle_count = cycle;")
        lines.append("")
        lines.append("        // Handle pending reconfiguration (pause-based V1)")
        lines.append("        // After barrier 3, old RM will check quit and exit.")
        lines.append("        // Other RMs will arrive at barrier 1 of next cycle and wait.")
        lines.append("        // We wait here for the new RM to signal ready.")
        lines.append("        if (reconfig_partition >= 0) {")
        lines.append("            ShmPartitionHeader* hdr = g_partition_headers[reconfig_partition];")
        lines.append("            // Wait for new RM to be ready")
        lines.append("            while (!shm_load32(&hdr->rm_ready)) {")
        lines.append("                // spin — Python starts new RM which sets rm_ready")
        lines.append("            }")
        lines.append("            // Clear reconfig state")
        lines.append("            shm_store32(&hdr->quit, 0);")
        lines.append("            shm_store32(&hdr->rm_ready, 0);")
        lines.append("            reconfig_partition = -1;")
        lines.append("            mailbox->cmd = CMD_NOOP;  // Signal completion to Python")
        lines.append("        }")
        lines.append("    }")
        lines.append("")

        # Quit: signal all RMs to exit via quit flags, do final barriers
        lines.append("    // Signal all RMs to quit")
        lines.append("    for (int p = 0; p < NUM_PARTITIONS; p++)")
        lines.append("        shm_store32(&g_partition_headers[p]->quit, 1);")
        lines.append("")
        lines.append("    // Do 3 more barrier rounds so RMs can see quit and exit")
        lines.append("    for (int i = 0; i < 3; i++)")
        lines.append("        barrier_wait(barrier, &local_sense);")
        lines.append("")

        # Cleanup
        lines.append("    // Cleanup")
        lines.append("    delete static_model;")
        lines.append("    delete static_ctx;")
        lines.append("    mailbox->sim_status = SIM_STATUS_DONE;")
        lines.append("    munmap(mailbox, SHM_MAILBOX_SIZE); close(mbox_fd);")
        lines.append("    munmap(barrier, 4096); close(barrier_fd);")
        lines.append("    for (int p = 0; p < NUM_PARTITIONS; p++) {")
        lines.append("        munmap(g_partition_bases[p], g_partition_sizes[p]);")
        lines.append("        close(ch_fds[p]);")
        lines.append("    }")
        lines.append("    return 0;")
        lines.append("}")

        path = self.dpi_dir / "static_driver.cpp"
        path.write_text("\n".join(lines))
        logger.info(f"Generated: {path}")

    # ── rm_driver_{variant}.cpp ────────────────────────────────────────

    def generate_rm_driver_cpp(
        self,
        part: PartitionInfo,
        rm: Dict[str, Any],
        trace: bool = False,
        trace_type: str = 'vcd',
    ):
        """Generate a single-threaded RM driver for one RM variant."""
        model_type = f"V{rm['wrapper_name']}"
        variant_name = rm['name']
        lines = []
        lines.append(f"// RM driver for {variant_name} (partition {part.name})")
        lines.append("// Generated by partial_reconfiguration")
        lines.append("")
        lines.append('#include "dpi_shm_channel.h"')
        lines.append('#include "barrier_sync.h"')
        lines.append("")
        lines.append("#include <verilated.h>")
        if trace:
            if trace_type == 'fst':
                lines.append("#include <verilated_fst_c.h>")
            else:
                lines.append("#include <verilated_vcd_c.h>")
        lines.append(f'#include "{model_type}.h"')
        lines.append("")
        lines.append("#include <cstring>")
        lines.append("#include <iostream>")
        lines.append("#include <sys/mman.h>")
        lines.append("#include <sys/stat.h>")
        lines.append("#include <fcntl.h>")
        lines.append("#include <unistd.h>")
        lines.append("")
        lines.append("// Required by Verilator")
        lines.append("double sc_time_stamp() { return 0; }")
        lines.append("")

        # Globals (exported for dpi_rm_{part}.cpp to use)
        lines.append("// Exported for DPI functions in dpi_rm_*.cpp")
        lines.append("void* g_channel_base = nullptr;")
        lines.append("ShmPartitionHeader* g_channel_header = nullptr;")
        lines.append("")

        # mmap helper
        lines.append("static void* mmap_file(const char* path, size_t expected_size, int* fd_out) {")
        lines.append("    int fd = open(path, O_RDWR);")
        lines.append("    if (fd < 0) { perror(path); return nullptr; }")
        lines.append("    struct stat st;")
        lines.append("    fstat(fd, &st);")
        lines.append("    size_t sz = (expected_size > 0) ? expected_size : (size_t)st.st_size;")
        lines.append("    void* ptr = mmap(nullptr, sz, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);")
        lines.append("    if (ptr == MAP_FAILED) { perror(\"mmap\"); close(fd); return nullptr; }")
        lines.append("    if (fd_out) *fd_out = fd; else close(fd);")
        lines.append("    return ptr;")
        lines.append("}")
        lines.append("")

        # main
        lines.append("int main(int argc, char** argv) {")
        lines.append('    const char* shm_dir = nullptr;')
        lines.append("    int partition_index = -1;")
        lines.append("    for (int i = 1; i < argc; i++) {")
        lines.append('        if (std::string(argv[i]) == "--shm-dir" && i + 1 < argc)')
        lines.append('            shm_dir = argv[++i];')
        lines.append('        else if (std::string(argv[i]) == "--partition-index" && i + 1 < argc)')
        lines.append("            partition_index = std::stoi(argv[++i]);")
        lines.append("    }")
        lines.append('    if (!shm_dir || partition_index < 0) {')
        lines.append('        std::cerr << "Usage: rm_binary --shm-dir DIR --partition-index N" << std::endl;')
        lines.append("        return 1;")
        lines.append("    }")
        lines.append("")
        lines.append("    std::string dir(shm_dir);")
        lines.append("")

        # Open channel
        lines.append("    // Open partition channel")
        lines.append("    int ch_fd = -1;")
        lines.append('    std::string ch_path = dir + "/partition_" + std::to_string(partition_index) + ".shm";')
        lines.append("    g_channel_base = mmap_file(ch_path.c_str(), 0, &ch_fd);")
        lines.append("    if (!g_channel_base) return 1;")
        lines.append("    g_channel_header = shm_header(g_channel_base);")
        lines.append("    struct stat ch_st; fstat(ch_fd, &ch_st);")
        lines.append("    size_t ch_size = ch_st.st_size;")
        lines.append("")

        # Open barrier
        lines.append("    // Open barrier")
        lines.append("    int br_fd = -1;")
        lines.append('    ShmBarrier* barrier = (ShmBarrier*)mmap_file((dir + "/barrier.shm").c_str(), 4096, &br_fd);')
        lines.append("    if (!barrier) return 1;")
        lines.append("")

        # Create model
        lines.append("    // Create RM model")
        lines.append("    auto ctx = new VerilatedContext;")
        lines.append(f"    auto model = new {model_type}(ctx);")
        lines.append("")

        # Init barrier sense
        lines.append("    // Init barrier sense from current global sense")
        lines.append("    uint32_t local_sense = __atomic_load_n(&barrier->sense, __ATOMIC_ACQUIRE);")
        lines.append("")

        # Reset assertion before rm_ready (if configured)
        if part.reset_name is not None and part.reset_behavior != 'none_intel':
            assert_val = 0 if part.reset_polarity == 'negative' else 1
            deassert_val = 1 if part.reset_polarity == 'negative' else 0
            lines.append(f"    // Assert reset for {part.reset_cycles} cycles before signaling ready")
            lines.append(f"    model->{part.reset_name} = {assert_val};")
            lines.append(f"    for (int rst_cycle = 0; rst_cycle < {part.reset_cycles}; rst_cycle++) {{")
            for clk in part.clock_names:
                lines.append(f"        model->{clk} = 0;")
            lines.append(f"        model->eval();")
            for clk in part.clock_names:
                lines.append(f"        model->{clk} = 1;")
            lines.append(f"        model->eval();")
            lines.append(f"    }}")
            lines.append(f"    model->{part.reset_name} = {deassert_val};")
            lines.append(f"    model->eval();")
            lines.append("")

        lines.append("    // Signal ready to static binary")
        lines.append("    shm_store32(&g_channel_header->rm_ready, 1);")
        lines.append("")

        # Main loop — use configured clock names
        lines.append("    // Main simulation loop")
        lines.append("    while (true) {")
        lines.append("        // Check quit before barrier (allows clean exit during reconfig)")
        lines.append("        if (shm_load32(&g_channel_header->quit))")
        lines.append("            break;")
        lines.append("")
        for clk in part.clock_names:
            lines.append(f"        model->{clk} = 0;")
        lines.append("        model->eval();")
        lines.append("        barrier_wait(barrier, &local_sense);  // 1: negedge done")
        lines.append("")
        lines.append("        barrier_wait(barrier, &local_sense);  // 2: swap done")
        lines.append("")
        for clk in part.clock_names:
            lines.append(f"        model->{clk} = 1;")
        lines.append("        model->eval();")
        lines.append("        barrier_wait(barrier, &local_sense);  // 3: posedge done")
        lines.append("    }")
        lines.append("")

        # Cleanup
        lines.append("    // Cleanup")
        lines.append("    delete model;")
        lines.append("    delete ctx;")
        lines.append("    munmap(g_channel_base, ch_size); close(ch_fd);")
        lines.append("    munmap(barrier, 4096); close(br_fd);")
        lines.append("    return 0;")
        lines.append("}")

        path = self.dpi_dir / f"rm_driver_{variant_name}.cpp"
        path.write_text("\n".join(lines))
        logger.info(f"Generated: {path}")

    # ── dpi_static_{partition}.cpp ─────────────────────────────────────

    def generate_dpi_static_partition_cpp(self, part: PartitionInfo):
        """Generate static-side DPI functions for one partition."""
        lines = []
        lines.append(f'// Static-side DPI functions for partition: {part.name}')
        lines.append(f'// Generated by partial_reconfiguration')
        lines.append('')
        lines.append('#include "dpi_shm_channel.h"')
        lines.append('#include <svdpi.h>')
        lines.append('')
        lines.append('// Partition base from static_driver.cpp')
        lines.append('extern void* g_partition_bases[];')
        lines.append('')
        lines.append('extern "C" {')
        lines.append('')

        # to_rm: static sends (write to outbox) — chunked for wide ports
        to_slot = 0
        for i, port in enumerate(part.to_rm_ports):
            w = port.get('width', 32)
            nc = _num_chunks(w)
            for c in range(nc):
                cw = _chunk_width(w, c)
                ct = _cpp_type(cw)
                suffix = f"_chunk{c}_send" if nc > 1 else "_send"
                fname = f"dpi_static_{part.name}_{port['name']}{suffix}"
                lines.append(f"void {fname}({ct} data) {{")
                lines.append(f"    ShmPort* p = shm_to_rm_outbox(g_partition_bases[{part.index}], {to_slot});")
                lines.append(f"    p->data = static_cast<uint64_t>(data{_mask_expr(cw)});")
                lines.append(f"    p->valid = 1;")
                lines.append("}")
                lines.append("")
                to_slot += 1

        # from_rm: static receives (read from inbox) — chunked for wide ports
        num_to_rm_slots = _total_slots(part.to_rm_ports)
        from_slot = 0
        for i, port in enumerate(part.from_rm_ports):
            w = port.get('width', 32)
            nc = _num_chunks(w)
            for c in range(nc):
                cw = _chunk_width(w, c)
                ct = _cpp_type(cw)
                suffix_d = f"_chunk{c}_recv_data" if nc > 1 else "_recv_data"
                suffix_v = f"_chunk{c}_recv_valid" if nc > 1 else "_recv_valid"
                ch_expr = f"shm_from_rm_inbox(g_partition_bases[{part.index}], {num_to_rm_slots}, {from_slot})"
                lines.append(f"{ct} dpi_static_{part.name}_{port['name']}{suffix_d}() {{")
                lines.append(f"    return static_cast<{ct}>({ch_expr}->data{_mask_expr(cw)});")
                lines.append("}")
                lines.append("")
                lines.append(f"int dpi_static_{part.name}_{port['name']}{suffix_v}() {{")
                lines.append(f"    return {ch_expr}->valid ? 1 : 0;")
                lines.append("}")
                lines.append("")
                from_slot += 1

        lines.append("} // extern \"C\"")

        path = self.dpi_dir / f"dpi_static_{part.name}.cpp"
        path.write_text("\n".join(lines))
        logger.info(f"Generated: {path}")

    # ── dpi_rm_{partition}.cpp ─────────────────────────────────────────

    def generate_dpi_rm_partition_cpp(self, part: PartitionInfo):
        """Generate RM-side DPI functions for one partition."""
        lines = []
        lines.append(f'// RM-side DPI functions for partition: {part.name}')
        lines.append(f'// Generated by partial_reconfiguration')
        lines.append('')
        lines.append('#include "dpi_shm_channel.h"')
        lines.append('#include <svdpi.h>')
        lines.append('')
        lines.append('// Channel base from rm_driver_*.cpp')
        lines.append('extern void* g_channel_base;')
        lines.append('extern ShmPartitionHeader* g_channel_header;')
        lines.append('')
        lines.append('extern "C" {')
        lines.append('')

        # to_rm: RM receives (read from inbox, check override) — chunked for wide ports
        to_slot = 0
        for i, port in enumerate(part.to_rm_ports):
            w = port.get('width', 32)
            nc = _num_chunks(w)
            for c in range(nc):
                cw = _chunk_width(w, c)
                ct = _cpp_type(cw)
                suffix_d = f"_chunk{c}_recv_data" if nc > 1 else "_recv_data"
                suffix_v = f"_chunk{c}_recv_valid" if nc > 1 else "_recv_valid"
                inbox_expr = f"shm_to_rm_inbox(g_channel_base, {to_slot})"
                ovr_expr = f"shm_to_rm_override(g_channel_base, {to_slot})"

                lines.append(f"{ct} dpi_rm_{part.name}_{port['name']}{suffix_d}() {{")
                lines.append(f"    ShmPortOverride* ovr = {ovr_expr};")
                lines.append(f"    if (shm_load32(&ovr->active))")
                lines.append(f"        return static_cast<{ct}>(shm_load64_relaxed(&ovr->value){_mask_expr(cw)});")
                lines.append(f"    return static_cast<{ct}>({inbox_expr}->data{_mask_expr(cw)});")
                lines.append("}")
                lines.append("")
                lines.append(f"int dpi_rm_{part.name}_{port['name']}{suffix_v}() {{")
                lines.append(f"    ShmPortOverride* ovr = {ovr_expr};")
                lines.append(f"    if (shm_load32(&ovr->active))")
                lines.append(f"        return 1;")
                lines.append(f"    return {inbox_expr}->valid ? 1 : 0;")
                lines.append("}")
                lines.append("")
                to_slot += 1

        # from_rm: RM sends (write to outbox) — chunked for wide ports
        from_slot = 0
        for i, port in enumerate(part.from_rm_ports):
            w = port.get('width', 32)
            nc = _num_chunks(w)
            for c in range(nc):
                cw = _chunk_width(w, c)
                ct = _cpp_type(cw)
                suffix = f"_chunk{c}_send" if nc > 1 else "_send"
                ch_expr = f"shm_from_rm_outbox(g_channel_base, g_channel_header->num_to_rm, {from_slot})"
                lines.append(f"void dpi_rm_{part.name}_{port['name']}{suffix}({ct} data) {{")
                lines.append(f"    ShmPort* p = {ch_expr};")
                lines.append(f"    p->data = static_cast<uint64_t>(data{_mask_expr(cw)});")
                lines.append(f"    p->valid = 1;")
                lines.append("}")
                lines.append("")
                from_slot += 1

        lines.append("} // extern \"C\"")

        path = self.dpi_dir / f"dpi_rm_{part.name}.cpp"
        path.write_text("\n".join(lines))
        logger.info(f"Generated: {path}")
