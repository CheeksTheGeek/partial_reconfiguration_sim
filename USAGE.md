# The Complete Partial Reconfiguration Flow — Exhaustive Walkthrough

Using the multi example: 3 partitions, 7 RMs, multi-process Verilator simulation.

---
## PHASE 0: What Problem This Solves

On a real FPGA with Dynamic Function eXchange (DFX), you can swap out a region of logic at runtime — the "reconfigurable partition" — while the rest
of the chip (the "static region") keeps running. This framework simulates that exact behavior using Verilator, so you can develop and test your PR
designs without hardware.

The fundamental challenge: Verilator compiles a single monolithic C++ model. You can't swap modules at runtime. The solution is a multi-process
architecture where the static region and each RM run as separate OS processes, communicating through shared memory, synchronized by a barrier — so
from each module's perspective, it's just seeing signals change every clock cycle, exactly like real hardware.

---
## PHASE 1: Configuration Loading

```python
# test.py line 280
with PRSystem(config='pr_config.yaml') as system:
```

### Step 1.1: PRConfig.load() parses the YAML

`config.py:71-122` — Detects .yaml extension, loads with PyYAML, calls `from_dict()`.

### Step 1.2: _validate_and_load() validates everything

`config.py:148-180` — Checks version is `'1.0'`, verifies required fields (`partitions`, `reconfigurable_modules`), then calls `_apply_defaults()`.

### Step 1.3: _apply_defaults() fills in smart defaults

`config.py:182-230` — This is critical. For the multi example:

```yaml
static_region:
  clocks: not specified
  auto_wrap_config:
    clock_name: clk
```

The code does:
```python
if 'clocks' not in self.static_region:
    awc = self.static_region.get('auto_wrap_config', {})
    if 'clock_name' in awc:
        self.static_region['clocks'] = [awc['clock_name']]  # → ['clk']
    else:
        self.static_region['clocks'] = ['clk']
```

Each RM also gets defaults: `design` defaults to `name`, `clocks` defaults to `['clk']`, `resets` defaults to `[{name: 'rst_n', polarity: 'negative'}]`.

### Step 1.4: PRSystem.__init__() creates the object graph

`system.py:44-126` — Stores config, creates:
- `self._builder = VerilatorBuilder(build_dir='build')`
- Empty dicts for `self.partitions`, `self.modules`
- Calls `self._setup_from_config()`

### Step 1.5: _setup_from_config() builds the object tree

`system.py:227-290` — Walks the config and creates:

```
PRSystem
├── StaticRegion("static_region")
│     sources: [rtl/static_region.sv]
│     auto_wrap: True
│
├── Partition("rp0")
│     interface: {counter: from_rm, width=32}
│     initial_rm: "counter_rm"
│     ├── ReconfigurableModule("counter_rm")
│     └── ReconfigurableModule("passthrough_rm")
│
├── Partition("rp1")
│     interface: {operand_a: to_rm/32, operand_b: to_rm/32, result: from_rm/32}
│     initial_rm: "adder_rm"
│     ├── ReconfigurableModule("adder_rm")
│     ├── ReconfigurableModule("subtractor_rm")
│     ├── ReconfigurableModule("xor_cipher_rm")
│     └── ReconfigurableModule("sub_cipher_rm")
│
└── Partition("rp_echo")
      interface: {static_counter_in: to_rm/32, rm_counter: from_rm/32, ...}
      initial_rm: "echo_counter_rm"
      └── ReconfigurableModule("echo_counter_rm")
```

Each `Partition` gets `register_rm()` called for all its RMs. Each `ReconfigurableModule` stores its `partition_name`, `design`, `sources`, and `auto_wrap` flag.

---
## PHASE 2: Build

```python
system.build()
```

`system.py:477-542` — Calls `self._setup_builder()` then `self._builder.build()`.

### Step 2.1: _setup_builder() feeds everything to VerilatorBuilder

`system.py:544-689` — This is the bridge between the high-level config and the low-level codegen. It:

#### 2.1a: Detects the static region's clock via pyslang

```python
from .verilator_builder import _detect_clock_from_rtl
static_clock = _detect_clock_from_rtl(static_sources, 'static_region')
```

`verilator_builder.py:140-208` — The 4-strategy detection:

1. **Parse with pyslang**: `_slang_compile()` calls `pyslang.syntax.SyntaxTree.fromFile()` on `rtl/static_region.sv`, creates a `pyslang.ast.Compilation`, calls `comp.getAllDiagnostics()` to trigger elaboration.
2. **Gather 1-bit inputs**: Walks `inst.body` members, finds ports with `member.kind == pyslang.ast.SymbolKind.Port`, checks `member.internalSymbol.type.bitWidth == 1` and `member.direction == pyslang.ast.ArgumentDirection.In`. For `static_region`, finds just `clk`.
3. **Single 1-bit input**: Only one found → `clk` is unambiguously the clock. Returns `'clk'`.

If there were multiple 1-bit inputs (e.g., `clk` and `rst_n`), it would call `_find_posedge_clocks()` which walks every `ProceduralBlock` member, checks `body.kind == StatementKind.Timed`, examines `body.timing.kind == TimingControlKind.SignalEvent`, checks `event.edge == EdgeKind.PosEdge`, and calls `event.expr.getSymbolReference()` to get the actual symbol name — pure pyslang AST, no regex.

#### 2.1b: Registers static region with builder

```python
self._builder.set_static_region(
    design_name='static_region',
    sources=['rtl/static_region.sv'],
    ports=[{name: 'activity_counter', width: 32, direction: 'output'}, ...],
    clock_name='clk',
)
```

#### 2.1c: Registers partitions

For each partition in config, extracts `to_rm_ports` and `from_rm_ports` from the boundary definition, collects RM variants, and calls `self._builder.add_partition()`. For rp1:

```python
self._builder.add_partition(
    name='rp1', index=1,
    rm_module_name='adder_rm',
    clock_name='clk',
    to_rm_ports=[{name: 'operand_a', width: 32}, {name: 'operand_b', width: 32}],
    from_rm_ports=[{name: 'result', width: 32}],
    rm_variants=[
        {name: 'adder_rm', design: 'adder_rm', wrapper_name: 'adder_rm_dpi_wrapper', index: 0},
        {name: 'subtractor_rm', design: 'subtractor_rm', wrapper_name: 'subtractor_rm_dpi_wrapper', index: 1},
        {name: 'xor_cipher_rm', ...},
        {name: 'sub_cipher_rm', ...},
    ],
    initial_rm_index=0,
)
```

### Step 2.2: VerilatorBuilder.build() — the 4-stage pipeline

`verilator_builder.py:306-363`

#### Stage 1: Generate DPI Bridges (_generate_bridges())

`verilator_builder.py:379-426` — For each partition:

**1a. Validate boundary against RTL with pyslang:**

`_validate_boundary_with_pyslang()` parses the first RM's RTL (e.g., `adder_rm.sv`), extracts its actual port widths, and compares against the config. If the config says `width: 32` but the RTL declares `[15:0]`, it logs a warning and auto-corrects to the RTL truth. This catches mismatches before they become cryptic Verilator errors.

**1b. Generate static-side DPI bridge:**

`dpi_bridge_generator.py:107-191` — For partition rp1, generates `build/bridges/adder_rm.sv`:

```systemverilog
// This module has the SAME NAME as the RM — it's a drop-in replacement
module adder_rm (
    input wire clk,
    input wire [31:0] operand_a,    // Same ports as real adder_rm
    input wire [31:0] operand_b,
    output reg [31:0] result
);
    // DPI-C function declarations
    import "DPI-C" function void dpi_static_rp1_operand_a_send(input int data);
    import "DPI-C" function void dpi_static_rp1_operand_b_send(input int data);
    import "DPI-C" function int  dpi_static_rp1_result_recv_data();
    import "DPI-C" function int  dpi_static_rp1_result_recv_valid();

    // Every posedge: send inputs to RM via shared memory
    always @(posedge clk) begin
        dpi_static_rp1_operand_a_send(operand_a);
        dpi_static_rp1_operand_b_send(operand_b);
    end

    // Every posedge: receive outputs from RM via shared memory
    always @(posedge clk) begin
        if (dpi_static_rp1_result_recv_valid())
            result <= dpi_static_rp1_result_recv_data();
    end
endmodule
```

This is the key trick: the static region's RTL instantiates `adder_rm u_adder_rm(...)`. During simulation, THIS bridge module gets compiled instead, so the static region seamlessly talks to shared memory instead of real logic.

**1c. Generate RM-side DPI wrapper:**

`dpi_bridge_generator.py:193-298` — For each RM variant (adder, subtractor, xor_cipher, sub_cipher), generates a wrapper like `build/wrappers/adder_rm_dpi_wrapper.sv`:

```systemverilog
module adder_rm_dpi_wrapper (
    input wire clk           // Only port is clk — isolated from boundary
);
    // DPI-C functions to receive data FROM static region
    import "DPI-C" function int dpi_rm_rp1_operand_a_recv_data();
    import "DPI-C" function int dpi_rm_rp1_operand_a_recv_valid();
    import "DPI-C" function int dpi_rm_rp1_operand_b_recv_data();
    import "DPI-C" function int dpi_rm_rp1_operand_b_recv_valid();
    // DPI-C function to send data TO static region
    import "DPI-C" function void dpi_rm_rp1_result_send(input int data);

    // Internal signals with /* verilator public */ for C++ access
    reg  [31:0] operand_a /* verilator public */;
    reg  [31:0] operand_b /* verilator public */;
    wire [31:0] result    /* verilator public */;

    // Instantiate the REAL RM
    adder_rm u_rm (
        .clk(clk),
        .operand_a(operand_a),
        .operand_b(operand_b),
        .result(result)
    );

    // Drive RM inputs from DPI channels
    always @(posedge clk) begin
        if (dpi_rm_rp1_operand_a_recv_valid())
            operand_a <= dpi_rm_rp1_operand_a_recv_data();
        if (dpi_rm_rp1_operand_b_recv_valid())
            operand_b <= dpi_rm_rp1_operand_b_recv_data();
    end

    // Send RM outputs to static via DPI
    always @(posedge clk) begin
        dpi_rm_rp1_result_send(result);
    end
endmodule
```

The bridge sources get appended to the static region's source list. The wrapper + RM sources become a separate compilation unit.

#### Stage 2: Generate DPI C++ Code

`dpi_cpp_generator.py:87-106` — Generates 8 types of files:

**2a. dpi_shm_channel.h** — Shared memory layout structs:

```
Per-partition shared memory file layout:

[Offset 0]     ShmPartitionHeader (64 bytes, cache-aligned)
  magic (0x50525348 = "PRSH"), version (1),
  num_to_rm, num_from_rm, initialized, quit, rm_ready

[Offset 64]    to_rm[0].outbox    (ShmPort, 64B)
[Offset 128]   to_rm[0].inbox     (ShmPort, 64B)
[Offset 192]   to_rm[0].override  (ShmPortOverride, 64B)
[Offset 256]   to_rm[1].outbox    ...
...            (T ports x 192 bytes)

[Offset 64+Tx192]      from_rm[0].outbox  (64B)
[Offset 64+Tx192+64]   from_rm[0].inbox   (64B)
...                     (F ports x 128 bytes)

Total: page-aligned(64 + Tx192 + Fx128)
```

Each `ShmPort` is 64-byte cache-aligned to prevent false sharing:
```c
struct alignas(64) ShmPort {
    uint64_t data;    // supports up to 64-bit boundary ports
    uint32_t valid;
};
```

Why outbox + inbox? Two copies of each port exist so the static and RM processes can read/write simultaneously without data races. Every cycle, the static driver copies outbox → inbox (the "swap"), then both sides read from inbox / write to outbox freely.

Why override? Python needs to write port values from the host. The override slot lets Python set a value + active flag that takes priority over the normal inbox during reads.

```c
struct alignas(64) ShmPortOverride {
    uint64_t value;   // supports up to 64-bit override values
    uint32_t active;
};
```

**2b. barrier_sync.h** — Sense-reversing barrier:

```c
struct ShmBarrier {
    alignas(64) volatile uint64_t cycle_count;   // offset 0
    alignas(64) volatile uint32_t count;         // offset 64
    alignas(64) volatile uint32_t num_processes; // offset 128
    alignas(64) volatile uint32_t sense;         // offset 192
    alignas(64) volatile uint32_t initialized;   // offset 256
};
```

The `barrier_wait()` function implements a sense-reversing barrier: each process flips its local sense, atomically increments count, and if it's the last arrival, resets count and flips global sense. Otherwise spins until global sense matches local. This ensures all N+1 processes (1 static + N RM) complete each phase before any moves to the next.

**2c. shm_mailbox.h** — Python <-> C++ command protocol:

```c
struct ShmMailbox {
    volatile uint32_t sim_status;  // SIM_STATUS_RUNNING etc.
    uint32_t _pad0;
    volatile uint64_t cycle_count;
    volatile uint32_t cmd;         // CMD_READ, CMD_WRITE, CMD_RECONFIG, CMD_QUIT
    volatile uint32_t target;      // 0=static, 1+=partition
    volatile uint32_t port_idx;
    volatile uint32_t rm_idx;
    volatile uint64_t write_value;
    volatile uint64_t read_value;
};
```

Protocol: Python writes target/port_idx/write_value, then writes cmd LAST (ordering matters). C++ polls cmd, processes it, writes read_value, sets cmd = CMD_NOOP. Python polls until NOOP, reads result.

**2d. signal_access.h** — Generated port accessor switch statements:

```c
inline uint64_t read_static_port(Vstatic_region* model, int port_idx) {
    switch (port_idx) {
        case 0: return static_cast<uint64_t>(model->activity_counter);
        case 1: return static_cast<uint64_t>(model->computed_result);
        case 2: return static_cast<uint64_t>(model->rp0_counter);
        // ...
        default: return 0;
    }
}

inline uint64_t read_rp1_port(int port_idx) {
    void* base = g_partition_bases[1];
    ShmPartitionHeader* hdr = shm_header(base);
    switch (port_idx) {
        case 0: {  // operand_a (to_rm)
            ShmPortOverride* ovr = shm_to_rm_override(base, 0);
            if (shm_load32(&ovr->active)) return shm_load64_relaxed(&ovr->value);
            return shm_load64_relaxed(&shm_to_rm_inbox(base, 0)->data);
        }
        case 1: { /* operand_b */ }
        case 2: return shm_load64_relaxed(
            &shm_from_rm_outbox(base, hdr->num_to_rm, 0)->data);  // result
        default: return 0;
    }
}
```

**2e. static_driver.cpp** — The static binary's main():

```
STATIC DRIVER MAIN LOOP (runs forever until CMD_QUIT)

while (!quit) {
  --- PHASE 1: NEGEDGE ---
  static_model->clk = 0;    // Negedge
  static_model->eval();      // Evaluate combinational
  barrier_wait();            // Wait for all RM processes

  --- PHASE 2: SWAP + COMMANDS ---
  swap_channels();           // Copy outbox -> inbox
  process_commands();        // Handle Python read/write
  barrier_wait();            // All processes synced

  --- PHASE 3: POSEDGE ---
  static_model->clk = 1;    // Posedge
  static_model->eval();      // Evaluate + latch
  barrier_wait();            // All processes done

  cycle++;
  mailbox->cycle_count = cycle;

  if (reconfig_partition >= 0) {
    // Wait for new RM binary to set rm_ready=1
    while (!hdr->rm_ready) { spin; }
    // Clear reconfig state, signal completion to Python
    mailbox->cmd = CMD_NOOP;
  }
}
```

The `swap_channels()` function iterates every port of every partition and copies outbox.data → inbox.data, outbox.valid → inbox.valid, then clears outbox.valid. This is the "double-buffering" that prevents read/write races.

**2f. rm_driver_adder_rm.cpp** — Each RM binary's main():

```
RM DRIVER (e.g., adder_rm)

1. mmap partition channel + barrier
2. Create Verilator model (Vadder_rm_dpi_wrapper)
3. Set rm_ready = 1

while (true) {
  if (quit flag set) break;

  model->clk = 0;  eval();  barrier_wait();  // P1
  barrier_wait();                             // P2
  model->clk = 1;  eval();  barrier_wait();  // P3
}

Cleanup and exit.
```

The RM driver doesn't do swap or command processing — that's the static driver's job. The RM just evaluates its model in lockstep with the barrier.

**2g. dpi_static_rp1.cpp** — Static-side DPI C functions:

```c
extern "C" {
void dpi_static_rp1_operand_a_send(int data) {
    ShmPort* p = shm_to_rm_outbox(g_partition_bases[1], 0);
    p->data = static_cast<uint64_t>(data);
    p->valid = 1;
}
int dpi_static_rp1_result_recv_data() {
    return static_cast<int>(
        shm_from_rm_inbox(g_partition_bases[1], 2, 0)->data
    );
}
}
```

When Verilator evaluates the bridge module's `always @(posedge clk)` block and calls `dpi_static_rp1_operand_a_send(operand_a)`, it executes THIS C function, which writes to the shared memory outbox.

**2h. dpi_rm_rp1.cpp** — RM-side DPI C functions:

```c
extern "C" {
int dpi_rm_rp1_operand_a_recv_data() {
    ShmPortOverride* ovr = shm_to_rm_override(g_channel_base, 0);
    if (shm_load32(&ovr->active))
        return static_cast<int>(shm_load64_relaxed(&ovr->value));
    return static_cast<int>(shm_to_rm_inbox(g_channel_base, 0)->data);
}
void dpi_rm_rp1_result_send(int data) {
    ShmPort* p = shm_from_rm_outbox(g_channel_base,
                                     g_channel_header->num_to_rm, 0);
    p->data = static_cast<uint64_t>(data);
    p->valid = 1;
}
}
```

Note the override check: if Python has written a value via `write_port()`, the override slot takes priority over the normal inbox data.

#### Stage 3: Generate Makefile

`makefile_generator.py:37-294` — Generates a Makefile with these targets:

```makefile
# Verilator compiles static region (with bridge modules replacing RMs)
Vstatic_region__ALL.a: static_region.sv + counter_rm_bridge.sv
                       + adder_rm_bridge.sv + echo_counter_rm_bridge.sv

# Verilator compiles each RM wrapper separately
Vcounter_rm_dpi_wrapper__ALL.a:    counter_rm_dpi_wrapper.sv + counter_rm.sv
Vpassthrough_rm_dpi_wrapper__ALL.a: passthrough_rm_dpi_wrapper.sv + passthrough_rm.sv
Vadder_rm_dpi_wrapper__ALL.a:      adder_rm_dpi_wrapper.sv + adder_rm.sv
# ... (7 RM libs total)

# Link static binary
static_binary: static_driver.o + dpi_static_rp0.o + dpi_static_rp1.o
               + dpi_static_rp_echo.o + Vstatic_region__ALL.a + verilated.o

# Link each RM binary
rm/counter_rm/rm_binary:    rm_driver_counter_rm.o + dpi_rm_rp0.o
                            + Vcounter_rm_dpi_wrapper__ALL.a + verilated.o
rm/adder_rm/rm_binary:      rm_driver_adder_rm.o + dpi_rm_rp1.o
                            + Vadder_rm_dpi_wrapper__ALL.a + verilated.o
# ... (7 RM binaries total)
```

#### Stage 4: Run Make

`verilator_builder.py:471-489` — Runs `make -j -f build/Makefile all`. This compiles everything in parallel:

1. Verilator converts each SV module set → C++ → `.a` library archive
2. C++ compiler builds DPI functions and drivers → `.o` object files
3. Linker produces 8 separate binaries: 1 `static_binary` + 7 `rm/*/rm_binary`

After build, `_binary_paths` maps:
```python
{
    'static':             Path('build/static_binary'),
    'rm/counter_rm':      Path('build/rm/counter_rm/rm_binary'),
    'rm/passthrough_rm':  Path('build/rm/passthrough_rm/rm_binary'),
    'rm/adder_rm':        Path('build/rm/adder_rm/rm_binary'),
    'rm/subtractor_rm':   Path('build/rm/subtractor_rm/rm_binary'),
    'rm/xor_cipher_rm':   Path('build/rm/xor_cipher_rm/rm_binary'),
    'rm/sub_cipher_rm':   Path('build/rm/sub_cipher_rm/rm_binary'),
    'rm/echo_counter_rm': Path('build/rm/echo_counter_rm/rm_binary'),
}
```

---
## PHASE 3: Simulation Startup

```python
system.simulate()
```

`system.py:708-802` — Creates `SimulationProcessManager` and calls `start()`.

### Step 3.1: Create shared memory files

`sim_process.py:69-182`:

**3.1a. Command mailbox** (`build/shm/cmd_mailbox.shm`):
- `SharedMemoryInterface(path, create=True)` creates a 4096-byte mmap'd file
- Initialized to all zeros → sim_status = SIM_STATUS_INIT

**3.1b. Barrier** (`build/shm/barrier.shm`):
- 4096-byte file, initialized with:
```
cycle_count = 0
count = 0
num_processes = 4  (1 static + 3 initial RMs)
sense = 0
initialized = 1
```

**3.1c. Partition channels** — one per partition:
- `build/shm/partition_0.shm` for rp0 (size = align(64 + 0x192 + 1x128) = 4096)
- `build/shm/partition_1.shm` for rp1 (size = align(64 + 2x192 + 1x128) = 4096)
- `build/shm/partition_2.shm` for rp_echo (size = align(64 + 1x192 + 3x128) = 4096)

Each initialized with header: `magic=0x50525348, version=1, num_to_rm, num_from_rm, initialized=1, quit=0, rm_ready=0`.

### Step 3.2: Launch processes

**3.2a. Static binary:**
```
build/static_binary --shm-dir build/shm
```
This process mmaps the mailbox, barrier, and all 3 partition channels, creates the Verilator model `Vstatic_region`, sets `sim_status = SIM_STATUS_RUNNING`, and enters its main loop.

**3.2b. Initial RM binaries** (one per partition):
```
build/rm/counter_rm/rm_binary      --shm-dir build/shm --partition-index 0
build/rm/adder_rm/rm_binary        --shm-dir build/shm --partition-index 1
build/rm/echo_counter_rm/rm_binary --shm-dir build/shm --partition-index 2
```
Each mmaps its partition channel + barrier, creates its Verilator model, sets `rm_ready = 1`, and enters its barrier-synchronized loop.

### Step 3.3: Wait for readiness

Python polls `mailbox.sim_status` until it equals `SIM_STATUS_RUNNING`. Once the static driver sets this, all 4 processes are in their main loops, synchronized at the barrier.

### Step 3.4: The system is now running

The 4 processes are cycling in lockstep:

```
              Time ->
Static:   P1--P2--P3--P1--P2--P3--P1--P2--P3--...
counter:  P1--P2--P3--P1--P2--P3--P1--P2--P3--...
adder:    P1--P2--P3--P1--P2--P3--P1--P2--P3--...
echo:     P1--P2--P3--P1--P2--P3--P1--P2--P3--...
              |       |       |
           barrier  barrier  barrier
           waits    waits    waits
```

Every cycle, the static region's `activity_counter` increments, `operand_a` and `operand_b` update, the bridge writes them to shared memory, the swap copies them to inboxes, the adder RM reads them, computes `result = a + b`, writes result to its outbox, the next swap copies it back, and the bridge reads it into the static model's `result` wire → `computed_result` register.

---
## PHASE 4: Python API Access

```python
api = system.get_rm_api('rp0')
val = api.read_counter()
```

### Step 4.1: API class generation

`system.py:907-964` — First call triggers `ReconfigurableModule._generate_api()`:

`module.py:171-192` — Calls `_get_boundary_ports()` which reads the partition's boundary config and creates `PortSpec` objects:

```python
# For rp0's counter_rm:
ports = [PortSpec(name='counter', width=32, direction='from_rm', index=0)]
```

Then `ApiGenerator.generate_api_class()` (`api_generator.py:34-60`) dynamically generates Python code:

```python
class CounterRmAPI:
    def __init__(self, shm):
        self._shm = shm
    def read_counter(self):
        return self._shm.read_port(0)
    # (no write_counter because direction is from_rm -> read-only)
```

This code is `exec()`'d to create a live class, then instantiated with the partition's `SharedMemoryInterface`.

### Step 4.2: Reading a port value

```python
val = api.read_counter()
# -> self._shm.read_port(0)
```

`shm_interface.py:212-226`:
```python
def read_port(self, port_idx):
    return self._send_command(CMD_READ, port_idx=port_idx)
```

`_send_command()` (`shm_interface.py:180-210`):
1. Writes `target = 1` (partition rp0 is index 0, target = index+1 = 1)
2. Writes `port_idx = 0`
3. Writes `cmd = CMD_READ` (LAST — this triggers C++)
4. Polls until `cmd == CMD_NOOP` (100us sleep between polls)
5. Returns `read_value`

On the C++ side, `process_commands()` in `static_driver.cpp` sees `cmd=CMD_READ, target=1`, calls `read_rp0_port(0)` which reads from the shared memory channel's `from_rm` inbox for port 0 → gets the counter value → writes it to `mailbox->read_value` → sets `cmd = CMD_NOOP`.

### Step 4.3: Writing a port value (rp1 example)

```python
api1 = system.get_rm_api('rp1')
api1.write_operand_a(100)
```

This triggers `_send_command(CMD_WRITE, port_idx=0, write_value=100)`:
1. Python writes to mailbox
2. C++ `process_commands()` sees `CMD_WRITE, target=2`, calls `write_rp1_port(0, 100)`
3. `write_rp1_port()` in `signal_access.h` writes to the override slot: `shm_store64_relaxed(&ovr->value, 100)` and `shm_store32(&ovr->active, 1)`
4. On the next cycle, when the RM's DPI function `dpi_rm_rp1_operand_a_recv_data()` is called, it checks the override first: `if (shm_load32(&ovr->active)) return ovr->value` → returns 100 instead of the inbox value

---
## PHASE 5: Reconfiguration

```python
system.reconfigure('rp1', 'subtractor_rm')
```

`system.py:804-878` — This is where partial reconfiguration happens.

### Step 5.1: System-level reconfigure

Looks up the partition's RM index (`subtractor_rm` is index 1 in rp1's variant list), then calls `self._sim_process.reconfigure(partition_index=1, rm_idx=1, new_rm_binary=...)`.

### Step 5.2: SimulationProcessManager.reconfigure()

`sim_process.py:244-356` — The most complex coordination in the system:

```
RECONFIGURATION PROTOCOL

1. Python sends CMD_RECONFIG to mailbox
   (target=2, rm_idx=1)

2. Static driver's process_commands() sees CMD_RECONFIG
   -> sets partition_1 header quit=1
   -> remembers reconfig_partition=1
   -> does NOT set cmd=NOOP yet (stays pending)

3. On next barrier cycle, adder_rm process checks quit
   -> sees quit=1 -> breaks out of loop -> exits

4. Python detects old RM process has exited
   -> Clears quit=0, rm_ready=0 in partition header
   -> Starts new RM process:
     build/rm/subtractor_rm/rm_binary
       --shm-dir build/shm --partition-index 1

5. New RM process mmaps channel + barrier
   -> Creates Vsubtractor_rm_dpi_wrapper model
   -> Sets rm_ready=1

6. Static driver sees rm_ready=1
   -> Clears reconfig state
   -> Sets cmd=CMD_NOOP (signals completion to Python)

7. Python sees CMD_NOOP -> reconfiguration complete
   -> Waits 2 more barrier cycles for data propagation
   -> Updates partition.active_rm to subtractor_rm
```

The key insight: during reconfiguration, the barrier's `num_processes` stays the same (the old RM exits and the new one joins, maintaining the count). The static driver pauses at the reconfig check point until the new RM is ready, so the barrier never deadlocks.

After reconfiguration, `system.get_rm_api('rp1')` generates a new API class for `subtractor_rm` with the same port names but the result now computes `operand_a - operand_b`.

---
## PHASE 6: Static Region API

```python
static_api = system.get_static_api()
activity = static_api.read_activity_counter()
```

`system.py:1042-1094` — Same pattern as RM API but uses `target=0` (`TARGET_STATIC`). The `read_static_port()` function in `signal_access.h` directly reads from the Verilator model's signals (e.g., `model->activity_counter`), not from shared memory.

This proves the static region keeps running continuously even during RM swaps — the `activity_counter` keeps incrementing regardless of what's happening in the partitions.

---
## PHASE 7: Shutdown

```python
# PRSystem.__exit__() via context manager
system.terminate()
```

`system.py:1129-1151` → `sim_process.py:409-449`:

1. Python sends `CMD_QUIT` via mailbox
2. Static driver sees `CMD_QUIT`, sets `quit_global = true`
3. Static driver sets `quit=1` on ALL partition headers
4. Static driver does 3 final barrier rounds so all RMs see quit and exit
5. Static driver cleans up models, unmaps memory, exits
6. Python waits for all processes to exit (2-second timeout)
7. Python cleans up shared memory files

---
## COMPLETE DATA FLOW DIAGRAM

```
PYTHON PROCESS

PRSystem
+-- SharedMemoryInterface (mmap to cmd_mailbox.shm)
|   +-- read_port() / write_port() / reconfigure()
+-- ApiGenerator-created classes
    +-- CounterRmAPI.read_counter()             -> read_port(0)
    +-- AdderRmAPI.write_operand_a(val)         -> write_port(0, val)
    +-- StaticRegionAPI.read_activity_counter()  -> read_port(0)

 cmd_mailbox.shm              barrier.shm
+----------------------------+ +------------------+
| cmd | target | port_idx    | | count | sense     |
| write_value | read_value   | | num_processes     |
+----------------------------+ +------------------+
        ^                              ^
        | mmap                         | mmap
        v                              v
+--------------------------------------------------------------+
|  STATIC BINARY PROCESS                                        |
|                                                                |
|  Vstatic_region (Verilator model)                             |
|  +-- activity_counter (reg, increments every cycle)           |
|  +-- operand_a, operand_b (regs, derived from counter)       |
|  +-- computed_result (reg, latched from RM)                   |
|  |                                                             |
|  +-- u_counter_rm (BRIDGE module, not real RM)                |
|  |   +-- DPI calls -> partition_0.shm                         |
|  +-- u_adder_rm (BRIDGE module)                               |
|  |   +-- DPI calls -> partition_1.shm                         |
|  +-- u_echo_counter_rm (BRIDGE module)                        |
|      +-- DPI calls -> partition_2.shm                         |
+-----------------------------+--------------------------------+
                              |
      +-----------------------+-----------------------+
      |                       |                       |
      v                       v                       v
 partition_0.shm        partition_1.shm         partition_2.shm
+----------------+     +----------------+      +----------------+
| hdr            |     | hdr            |      | hdr            |
| counter:out/in |     | op_a:out/in/ovr|      | ctr_in:out/in  |
|                |     | op_b:out/in/ovr|      | ctr_in:ovr     |
|                |     | result:out/in  |      | rm_ctr:out/in  |
|                |     |                |      | echo:out/in    |
|                |     |                |      | latency:out/in |
+--------+-------+     +--------+-------+      +--------+-------+
         | mmap                 | mmap                  | mmap
         v                      v                       v
+----------------+     +----------------+      +----------------+
| COUNTER_RM     |     | ADDER_RM       |      | ECHO_CTR_RM    |
| BINARY         |     | BINARY         |      | BINARY         |
|                |     |                |      |                |
| Vcounter_rm_   |     | Vadder_rm_     |      | Vecho_ctr_     |
| dpi_wrapper    |     | dpi_wrapper    |      | dpi_wrapper    |
|   counter_rm   |     |   adder_rm     |      |   echo_ctr_rm  |
|   (real logic) |     |   (real logic) |      |   (real logic) |
+----------------+     +----------------+      +----------------+
         ^                      ^                       ^
         +---------- barrier.shm -----------------------+
                  (all 4 processes sync here)
```

---
## PHASE 8: One Complete Clock Cycle in Detail

Let's trace cycle N for partition rp1 (adder with operand_a=100, operand_b=42):

### BARRIER PHASE 1: NEGEDGE

**Static process:**
```
static_model->clk = 0
static_model->eval()
  -> Bridge module's combinational logic runs
  -> dpi_static_rp1_result_recv_data() is called
    -> reads partition_1.shm from_rm inbox[0].data = 142
    -> bridge sets result = 142
  -> static_region latches: computed_result = 142 (from previous posedge)
barrier_wait()  <- blocks until all 4 arrive
```

**Adder process:**
```
model->clk = 0
model->eval()
  -> Wrapper's combinational logic: result = operand_a + operand_b = 142
  -> dpi_rm_rp1_result_send(142) is called
    -> writes partition_1.shm from_rm outbox[0].data = 142, valid = 1
barrier_wait()  <- blocks until all 4 arrive
```

### BARRIER PHASE 2: SWAP + COMMANDS

**Static process:**
```
swap_channels()
  -> For partition_1:
    -> to_rm outbox[0].data -> inbox[0].data  (operand_a flows to RM)
    -> to_rm outbox[1].data -> inbox[1].data  (operand_b flows to RM)
    -> from_rm outbox[0].data -> inbox[0].data  (result=142 flows to static)
    -> Clear all outbox valid flags
process_commands()
  -> If Python sent CMD_READ for rp1/result:
    -> read_rp1_port(2) -> reads from_rm inbox = 142
    -> mailbox->read_value = 142
    -> mailbox->cmd = CMD_NOOP
barrier_wait()
```

**Adder process:**
```
(does nothing in phase 2, just barrier_wait)
```

### BARRIER PHASE 3: POSEDGE

**Static process:**
```
static_model->clk = 1
static_model->eval()
  -> Bridge's always @(posedge clk):
    dpi_static_rp1_operand_a_send(operand_a)
      -> writes partition_1.shm to_rm outbox[0].data = 100, valid = 1
    dpi_static_rp1_operand_b_send(operand_b)
      -> writes partition_1.shm to_rm outbox[1].data = 42, valid = 1
  -> static_region's always @(posedge clk):
    activity_counter <= activity_counter + 1
    computed_result <= result  (= 142)
barrier_wait()
```

**Adder process:**
```
model->clk = 1
model->eval()
  -> Wrapper's always @(posedge clk):
    if (dpi_rm_rp1_operand_a_recv_valid())  -> checks inbox valid = 1
      operand_a <= dpi_rm_rp1_operand_a_recv_data()  -> reads 100
    if (dpi_rm_rp1_operand_b_recv_valid())
      operand_b <= dpi_rm_rp1_operand_b_recv_data()  -> reads 42
  -> real adder_rm: result = operand_a + operand_b (combinational)
    -> next eval will produce 100 + 42 = 142
  -> dpi_rm_rp1_result_send(result)
    -> writes outbox with current result
barrier_wait()
```

### CYCLE N+1 BEGINS

---
## Summary of Every File's Role

| File | Role |
|------|------|
| `config.py` | Parse + validate YAML/JSON/TOML config, apply defaults |
| `system.py` | Top-level orchestrator: build -> simulate -> reconfigure -> terminate |
| `static.py` | StaticRegion wrapper: tracks ports, generates Python API |
| `partition.py` | Partition wrapper: tracks RMs, manages reconfiguration phases |
| `module.py` | ReconfigurableModule wrapper: tracks design/sources, generates API |
| `greybox.py` | Auto-generates tie-off modules for empty partitions |
| `validation.py` | Port compatibility checking (strict/superset/relaxed policies) |
| `verilator_builder.py` | Build pipeline: pyslang validation -> codegen -> make |
| `dpi_bridge_generator.py` | Generate SV bridge (static side) + wrapper (RM side) |
| `dpi_cpp_generator.py` | Generate all C++: SHM structs, drivers, DPI functions |
| `makefile_generator.py` | Generate multi-target Makefile |
| `api_generator.py` | Generate Python API classes with read/write methods |
| `shm_interface.py` | Python<->C++ IPC: mmap mailbox, command protocol |
| `sim_process.py` | Process lifecycle: launch, monitor, reconfigure, terminate |

---
## Port Width Support

Boundary ports support widths from 1 to 64 bits. The DPI-C type is selected automatically:

| Port Width | DPI-C Type | C++ Type | ShmPort.data Type |
|------------|-----------|----------|-------------------|
| 1-32 bits | `int` | `int` | `uint64_t` |
| 33-64 bits | `longint` | `long long` | `uint64_t` |
| >64 bits | **Not supported** | — | — |

Ports wider than 64 bits are rejected at codegen time with a `ValueError` recommending splitting into multiple ports.

The shared memory channel uses `uint64_t` for all port data regardless of width, so 48-bit and 64-bit values pass through without truncation. Mask expressions are applied at the DPI function boundary to zero-extend narrow signals to their DPI type width.

---
## Clock Detection

Clock signals are detected automatically from RTL using pyslang's AST analysis. The detection uses a 4-strategy chain that never fails for valid modules:

1. **Sole 1-bit input**: If the module has exactly one 1-bit input port, it's unambiguously the clock.
2. **Timing control analysis**: Walks `ProceduralBlock` members, examines `StatementKind.Timed` → `TimingControlKind.SignalEvent` → `EdgeKind.PosEdge`, and calls `expr.getSymbolReference()` to identify which 1-bit input is used in `posedge` contexts.
3. **Name-pattern matching**: Matches 1-bit input names against known patterns (`clk`, `clock`, `clk_i`, `clock_i`, `i_clk`, `i_clock`, `sys_clk`) and substrings.
4. **Last resort**: Uses the first 1-bit input port (with a warning).

If `auto_wrap_config.clock_name` is specified in the config, it is used to populate the default `clocks` list, providing an explicit fallback when auto-detection is not needed.
