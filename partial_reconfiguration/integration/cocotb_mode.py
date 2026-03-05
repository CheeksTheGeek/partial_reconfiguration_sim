"""
Cocotb integration mode for partial reconfiguration.

Provides:
- CtrlMailbox: SHM-backed control channel for cross-process reconfiguration.
- PROverlay: drop-in replacement for cocotbpynq.Overlay that adds PR support.
- pr_synctest: decorator for synchronous test functions.

Cross-process reconfiguration
------------------------------
cocotb tests run embedded inside a Verilator subprocess.  The live PRSystem
lives in the parent process (cocotb_runner.py).  Communication uses a tiny
SHM region (ctrl_mailbox.shm) inside the existing PR_SHM_DIR directory.

SHM layout (512 bytes):
  offset  0: uint32 magic     0xC0C0BA55
  offset  4: uint32 seq_req   test writes sequence number when submitting
  offset  8: uint32 seq_resp  control thread echoes seq_req when done
  offset 12: uint32 status    0=idle 1=pending 2=ok 3=error
  offset 16: char[64]  partition name (null-terminated)
  offset 80: char[64]  rm name (null-terminated)
  offset 144: char[256] error message (null-terminated)

Protocol:
  1. Test writes partition/rm/seq_req, then sets status=PENDING (release).
  2. Control thread polls status; on PENDING reads partition+rm, calls
     pr_system.reconfigure(), writes seq_resp, sets status=OK/ERROR.
  3. Test polls status+seq_resp; on completion resets to IDLE.
"""
import mmap
import os
import struct
import time
from typing import Optional


# ── CtrlMailbox ──────────────────────────────────────────────────────────────

class CtrlMailbox:
    """Shared-memory control mailbox for cross-process reconfiguration."""

    SIZE = 512
    MAGIC = 0xC0C0BA55

    STATUS_IDLE    = 0
    STATUS_PENDING = 1
    STATUS_OK      = 2
    STATUS_ERROR   = 3

    OFF_MAGIC     = 0
    OFF_SEQ_REQ   = 4
    OFF_SEQ_RESP  = 8
    OFF_STATUS    = 12
    OFF_PARTITION = 16
    OFF_RM_NAME   = 80
    OFF_ERROR_MSG = 144

    def __init__(self, path: str, create: bool = False):
        self.path = path
        if create:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC)
            os.ftruncate(fd, self.SIZE)
            self._mm = mmap.mmap(fd, self.SIZE)
            os.close(fd)
            self._mm.seek(0)
            self._mm.write(b'\x00' * self.SIZE)
            struct.pack_into('<I', self._mm, self.OFF_MAGIC, self.MAGIC)
            struct.pack_into('<I', self._mm, self.OFF_STATUS, self.STATUS_IDLE)
            self._mm.flush()
        else:
            fd = os.open(path, os.O_RDWR)
            self._mm = mmap.mmap(fd, self.SIZE)
            os.close(fd)

    def _read_u32(self, offset: int) -> int:
        return struct.unpack_from('<I', self._mm, offset)[0]

    def _write_u32(self, offset: int, value: int):
        struct.pack_into('<I', self._mm, offset, value)

    def _read_str(self, offset: int, max_len: int) -> str:
        raw = bytes(self._mm[offset:offset + max_len])
        return raw.split(b'\x00')[0].decode('utf-8', errors='replace')

    def _write_str(self, offset: int, max_len: int, value: str):
        encoded = value.encode('utf-8')[:max_len - 1]
        self._mm[offset:offset + max_len] = encoded + b'\x00' * (max_len - len(encoded))

    @property
    def status(self) -> int:
        return self._read_u32(self.OFF_STATUS)

    @property
    def seq_req(self) -> int:
        return self._read_u32(self.OFF_SEQ_REQ)

    @property
    def seq_resp(self) -> int:
        return self._read_u32(self.OFF_SEQ_RESP)

    @property
    def partition(self) -> str:
        return self._read_str(self.OFF_PARTITION, 64)

    @property
    def rm_name(self) -> str:
        return self._read_str(self.OFF_RM_NAME, 64)

    @property
    def error_msg(self) -> str:
        return self._read_str(self.OFF_ERROR_MSG, 256)

    def submit(self, partition: str, rm: str, seq: int):
        """Called by cocotb test: write request then set PENDING."""
        self._write_str(self.OFF_PARTITION, 64, partition)
        self._write_str(self.OFF_RM_NAME, 64, rm)
        self._write_u32(self.OFF_SEQ_REQ, seq)
        self._mm.flush()
        self._write_u32(self.OFF_STATUS, self.STATUS_PENDING)
        self._mm.flush()

    def complete_ok(self, seq: int):
        """Called by control thread on success."""
        self._write_u32(self.OFF_SEQ_RESP, seq)
        self._mm.flush()
        self._write_u32(self.OFF_STATUS, self.STATUS_OK)
        self._mm.flush()

    def complete_error(self, seq: int, msg: str):
        """Called by control thread on failure."""
        self._write_str(self.OFF_ERROR_MSG, 256, msg)
        self._write_u32(self.OFF_SEQ_RESP, seq)
        self._mm.flush()
        self._write_u32(self.OFF_STATUS, self.STATUS_ERROR)
        self._mm.flush()

    def reset(self):
        """Reset to IDLE after the test has consumed the response."""
        self._write_u32(self.OFF_STATUS, self.STATUS_IDLE)
        self._mm.flush()

    def close(self):
        self._mm.close()


# ── internal helpers ─────────────────────────────────────────────────────────

def _get_ctrl_mailbox() -> CtrlMailbox:
    path = os.environ.get('PR_CTRL_SHM')
    if not path:
        raise RuntimeError(
            "PR_CTRL_SHM not set. Use system.simulate_cocotb() to run tests."
        )
    return CtrlMailbox(path, create=False)


def _pr_reconfigure_sync(partition: str, rm: str, timeout_s: float = 30.0):
    """
    Synchronous reconfiguration via SHM control mailbox.

    Safe inside @pr_synctest / @cocotb.external (runs in a thread, blocking ok).
    """
    mb = _get_ctrl_mailbox()
    seq = int(time.time() * 1000) & 0xFFFFFF
    mb.submit(partition, rm, seq)

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status = mb.status
        if status == CtrlMailbox.STATUS_OK and mb.seq_resp == seq:
            mb.reset()
            return
        if status == CtrlMailbox.STATUS_ERROR and mb.seq_resp == seq:
            msg = mb.error_msg
            mb.reset()
            raise RuntimeError(f"Reconfigure {partition} → {rm} failed: {msg}")
        time.sleep(0.005)

    raise TimeoutError(f"Reconfigure {partition} → {rm} timed out after {timeout_s}s")


# ── async version for plain cocotb @test coroutines ─────────────────────────

async def pr_reconfigure(partition: str, rm: str, timeout_ns: float = 5_000_000):
    """
    Async reconfiguration for use inside plain cocotb @test coroutines.

    Polls using cocotb Timer ticks so the simulation clock keeps advancing
    (required for the barrier spin to complete) while we wait.
    """
    from cocotb.triggers import Timer

    mb = _get_ctrl_mailbox()
    seq = int(time.time() * 1000) & 0xFFFFFF
    mb.submit(partition, rm, seq)

    elapsed = 0.0
    poll_ns = 10_000
    while elapsed < timeout_ns:
        await Timer(poll_ns, unit='ns')
        elapsed += poll_ns
        status = mb.status
        if status == CtrlMailbox.STATUS_OK and mb.seq_resp == seq:
            mb.reset()
            return
        if status == CtrlMailbox.STATUS_ERROR and mb.seq_resp == seq:
            msg = mb.error_msg
            mb.reset()
            raise RuntimeError(f"Reconfigure {partition} → {rm} failed: {msg}")

    raise TimeoutError(
        f"Reconfigure {partition} → {rm} timed out after {timeout_ns/1e6:.1f} ms sim-time"
    )


# ── PROverlay ────────────────────────────────────────────────────────────────

def _patch_cocotb_for_v2():
    """
    Patch cocotb 2.0 to restore the API cocotbpynq depends on.

    cocotbpynq uses @cocotb.function and cocotb.external which were removed
    in cocotb 2.0.  Their replacements are cocotb._bridge.resume and
    cocotb._bridge.bridge respectively.
    """
    import cocotb
    if not hasattr(cocotb, 'function') or not hasattr(cocotb, 'external'):
        try:
            from cocotb._bridge import bridge, resume
            if not hasattr(cocotb, 'function'):
                cocotb.function = resume
            if not hasattr(cocotb, 'external'):
                cocotb.external = bridge
        except ImportError:
            pass


class PROverlay:
    """
    Drop-in replacement for cocotbpynq.Overlay with PR reconfiguration support.

    Wraps cocotbpynq.Overlay (MMIO/DMA discovery from HWH) and adds
    reconfigure() which talks to the parent process via SHM control mailbox.

    Parameters
    ----------
    bitfile_name : str
        Bitstream/design file name — used to find the .hwh file.
    dut_modtype : str, optional
        Override for HWH MODTYPE lookup.  Defaults to 'pr_cocotb_top'.
    """

    def __init__(self, bitfile_name: str = None, dut_modtype: str = 'pr_cocotb_top'):
        _patch_cocotb_for_v2()
        try:
            from cocotbpynq.overlay import Overlay
        except ImportError:
            raise ImportError(
                "cocotbpynq is required for PROverlay. "
                "Install it from ../cocotbpynq_release or via pip."
            )
        self._overlay = Overlay(bitfile_name=bitfile_name, dut_modtype=dut_modtype)

    def reconfigure(self, partition_name: str, rm_name: str):
        """Trigger PR reconfiguration via SHM control mailbox (blocking)."""
        _pr_reconfigure_sync(partition_name, rm_name)

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        return getattr(self._overlay, name)


# ── pr_synctest ──────────────────────────────────────────────────────────────

def pr_synctest(test_func):
    """
    Decorator: wrap a synchronous test function for cocotb + PR.

    Usage::

        @pr_synctest
        def test_my_design(dut):
            overlay = PROverlay('design.bit')
            mmio = MMIO(0x43C00000, 0x1000)
            mmio.write(0x00, 42)
            assert mmio.read(0x04) == 42 + 0x1000
            overlay.reconfigure('rp0', 'new_rm')
    """
    _patch_cocotb_for_v2()
    try:
        from cocotbpynq.simulator import synctest
        return synctest(test_func)
    except ImportError:
        from cocotb import test
        from cocotb._bridge import bridge
        qualname = test_func.__qualname__
        module   = test_func.__module__
        wrapped  = bridge(test_func)

        async def _async(dut):
            await wrapped(dut)

        result = test(_async)
        result.__module__   = module
        result.__qualname__ = qualname
        return result
