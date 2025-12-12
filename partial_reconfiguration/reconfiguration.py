from enum import Enum, auto
from typing import Optional, Callable, Any, TYPE_CHECKING
import time
import logging

if TYPE_CHECKING:
    from .partition import Partition

logger = logging.getLogger(__name__)


class ReconfigurationPhase(Enum):
    """
    Phases of the reconfiguration process.

    Models the actual phases that occur during FPGA partial reconfiguration.
    """

    ACTIVE = auto()
    """RM is running normally, processing transactions."""

    QUIESCE = auto()
    """
    Draining in-flight transactions before isolation.
    Static region stops sending new requests.
    Wait for pending responses to complete.
    """

    ISOLATE = auto()
    """
    RM boundary is gated/isolated.
    - Outputs from RM are held at safe values (typically 0)
    - Inputs to RM are ignored
    - Static region sees isolation, not RM
    """

    SWAP = auto()
    """
    Configuration bits being loaded.
    Old RM is removed, new RM bitstream is streaming in.
    This phase has duration (config_time_ms).
    """

    RESET = auto()
    """
    Applying reset to newly configured RM.
    - Xilinx: GSR (Global Set Reset) pulse
    - Intel: User must design reset logic
    - Simulation: Fresh process or explicit reset
    """

    ENABLE = auto()
    """
    Releasing isolation, RM becomes active.
    - Remove boundary gating
    - Allow transactions to flow
    - RM is now operational
    """


class ResetBehavior(Enum):
    """
    Reset behavior after reconfiguration.

    Different FPGA vendors have different behaviors.
    """

    FRESH = auto()
    """
    Fresh state - all registers at reset values.
    Default behavior, matches Xilinx GSR.
    In simulation: new process = fresh state automatically.
    """

    GSR_XILINX = auto()
    """
    Xilinx Global Set/Reset behavior.
    All registers go to their INIT values after reconfiguration.
    Same as FRESH for simulation purposes.
    """

    NONE_INTEL = auto()
    """
    Intel FPGA behavior - NO automatic reset.
    Registers retain whatever values they had (undefined after reconfig).
    User MUST design reset logic into the RM.
    In simulation: would need special handling to preserve garbage state.
    """


class ReconfigurationController:
    """
    Controls the reconfiguration state machine for a partition.

    This controller manages the phase transitions during reconfiguration,
    ensuring proper sequencing and timing that models real FPGA behavior.

    The key insight: real PR isn't instant. It has phases, timing, and
    handshaking requirements that must be modeled for accurate simulation.
    """

    def __init__(
        self,
        partition: 'Partition' = None,
        reset_behavior: ResetBehavior = ResetBehavior.FRESH,
        quiesce_timeout_ms: float = 100.0,
        isolation_setup_ms: float = 1.0,
        reset_cycles: int = 10
    ):
        """
        Initialize reconfiguration controller.

        Parameters
        ----------
        partition : Partition
            The partition this controller manages
        reset_behavior : ResetBehavior
            How to handle reset after reconfiguration
        quiesce_timeout_ms : float
            Maximum time to wait for transactions to drain
        isolation_setup_ms : float
            Time for isolation to stabilize
        reset_cycles : int
            Number of clock cycles to hold reset
        """
        self.partition = partition
        self.reset_behavior = reset_behavior
        self.quiesce_timeout_ms = quiesce_timeout_ms
        self.isolation_setup_ms = isolation_setup_ms
        self.reset_cycles = reset_cycles

        self._phase = ReconfigurationPhase.ACTIVE
        self._phase_start_time: Optional[float] = None

        self._on_phase_enter: Optional[Callable[[ReconfigurationPhase], None]] = None
        self._on_phase_exit: Optional[Callable[[ReconfigurationPhase], None]] = None

    @property
    def phase(self) -> ReconfigurationPhase:
        """Current reconfiguration phase."""
        return self._phase

    @property
    def is_active(self) -> bool:
        """True if RM is in normal operating state."""
        return self._phase == ReconfigurationPhase.ACTIVE

    @property
    def is_isolated(self) -> bool:
        """True if RM boundary is isolated."""
        return self._phase in (
            ReconfigurationPhase.ISOLATE,
            ReconfigurationPhase.SWAP,
            ReconfigurationPhase.RESET
        )

    @property
    def is_reconfiguring(self) -> bool:
        """True if reconfiguration is in progress."""
        return self._phase != ReconfigurationPhase.ACTIVE

    def _transition_to(self, new_phase: ReconfigurationPhase):
        """Transition to a new phase."""
        old_phase = self._phase

        if self._on_phase_exit:
            self._on_phase_exit(old_phase)

        self._phase = new_phase
        self._phase_start_time = time.time()

        logger.debug(f"Partition '{self.partition.name if self.partition else '?'}': "
                     f"{old_phase.name} -> {new_phase.name}")

        if self._on_phase_enter:
            self._on_phase_enter(new_phase)

    def begin_reconfiguration(self) -> bool:
        """
        Begin the reconfiguration process.

        Transitions from ACTIVE to QUIESCE phase.

        Returns
        -------
        bool
            True if transition succeeded
        """
        if self._phase != ReconfigurationPhase.ACTIVE:
            logger.warning(f"Cannot begin reconfiguration: already in {self._phase.name}")
            return False

        self._transition_to(ReconfigurationPhase.QUIESCE)
        return True

    def quiesce_complete(self) -> bool:
        """
        Signal that quiesce phase is complete.

        Called when all in-flight transactions have drained.
        Transitions from QUIESCE to ISOLATE.

        Returns
        -------
        bool
            True if transition succeeded
        """
        if self._phase != ReconfigurationPhase.QUIESCE:
            return False

        self._transition_to(ReconfigurationPhase.ISOLATE)
        return True

    def isolation_complete(self) -> bool:
        """
        Signal that isolation is established.

        Transitions from ISOLATE to SWAP.

        Returns
        -------
        bool
            True if transition succeeded
        """
        if self._phase != ReconfigurationPhase.ISOLATE:
            return False

        self._transition_to(ReconfigurationPhase.SWAP)
        return True

    def swap_complete(self) -> bool:
        """
        Signal that RM swap is complete.

        Called after new RM process has started.
        Transitions from SWAP to RESET.

        Returns
        -------
        bool
            True if transition succeeded
        """
        if self._phase != ReconfigurationPhase.SWAP:
            return False

        self._transition_to(ReconfigurationPhase.RESET)
        return True

    def reset_complete(self) -> bool:
        """
        Signal that reset phase is complete.

        Transitions from RESET to ENABLE.

        Returns
        -------
        bool
            True if transition succeeded
        """
        if self._phase != ReconfigurationPhase.RESET:
            return False

        self._transition_to(ReconfigurationPhase.ENABLE)
        return True

    def enable_complete(self) -> bool:
        """
        Signal that enable phase is complete.

        RM is now fully active and operational.
        Transitions from ENABLE to ACTIVE.

        Returns
        -------
        bool
            True if transition succeeded
        """
        if self._phase != ReconfigurationPhase.ENABLE:
            return False

        self._transition_to(ReconfigurationPhase.ACTIVE)
        return True

    def abort_reconfiguration(self) -> bool:
        """
        Abort an in-progress reconfiguration.

        Only valid during QUIESCE phase (before isolation).
        Returns to ACTIVE state.

        Returns
        -------
        bool
            True if abort succeeded
        """
        if self._phase == ReconfigurationPhase.QUIESCE:
            self._transition_to(ReconfigurationPhase.ACTIVE)
            return True
        return False

    def execute_full_sequence(
        self,
        swap_callback: Callable[[], Any],
        config_time_ms: float = 0.0
    ) -> bool:
        """
        Execute the full reconfiguration sequence.

        This is a convenience method that runs through all phases:
        ACTIVE -> QUIESCE -> ISOLATE -> SWAP -> RESET -> ENABLE -> ACTIVE

        IMPORTANT: Hardware isolation is handled by PRSystem.reconfigure(),
        which calls set_isolation() BEFORE and AFTER this method. The
        boundary.isolate()/release() calls here are just marker updates
        for debugging/state tracking.

        Parameters
        ----------
        swap_callback : callable
            Function to call during SWAP phase (terminates old, starts new)
        config_time_ms : float
            Configuration time to simulate (0 = instant)

        Returns
        -------
        bool
            True if reconfiguration completed successfully
        """
        if not self.begin_reconfiguration():
            return False

        # In simulation, quiesce is instant because:
        # 2. Hardware isolation in static_region.sv blocks new traffic
        # For cycle-accurate quiesce, implement transaction counting in RTL
        if not self.quiesce_complete():
            return False

        # Phase 2: ISOLATE
        # Hardware isolation is set by PRSystem.reconfigure() BEFORE this call
        # The boundary marker update is for debugging/state tracking only
        if self.partition and hasattr(self.partition, '_boundary'):
            self.partition._boundary.isolate()

        # Small delay for isolation to stabilize (wall-clock, not sim cycles)
        if self.isolation_setup_ms > 0:
            time.sleep(self.isolation_setup_ms / 1000.0)

        if not self.isolation_complete():
            return False

        # Phase 3: SWAP
        # Execute the actual swap (terminate old, start new)
        swap_callback()

        # Simulate configuration time (wall-clock approximation)
        if config_time_ms > 0:
            time.sleep(config_time_ms / 1000.0)

        if not self.swap_complete():
            return False

        # Phase 4: RESET
        # Fresh state is automatic (new process = fresh registers)
        # Matches Xilinx GSR behavior. For Intel behavior (no auto reset),
        # user must design reset logic into their RM.
        if not self.reset_complete():
            return False

        # Phase 5: ENABLE
        # Hardware isolation is released by PRSystem.reconfigure() AFTER this returns
        # The boundary marker update is for debugging/state tracking only
        if self.partition and hasattr(self.partition, '_boundary'):
            self.partition._boundary.release()

        if not self.enable_complete():
            return False

        return True

    def get_phase_duration_ms(self) -> float:
        """Get time spent in current phase (milliseconds)."""
        if self._phase_start_time is None:
            return 0.0
        return (time.time() - self._phase_start_time) * 1000.0

    def set_callbacks(
        self,
        on_enter: Callable[[ReconfigurationPhase], None] = None,
        on_exit: Callable[[ReconfigurationPhase], None] = None
    ):
        """Set callbacks for phase transitions."""
        self._on_phase_enter = on_enter
        self._on_phase_exit = on_exit

    def __repr__(self) -> str:
        return (f"<ReconfigurationController phase={self._phase.name} "
                f"reset={self.reset_behavior.name}>")
