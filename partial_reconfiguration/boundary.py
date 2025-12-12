import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class PartitionBoundary:
    """
    Lightweight boundary marker for a partition.
    This class tracks isolation state for debugging and provides
    API compatibility. The actual isolation is performed by hardware
    in the static region (controlled via PRSystem.set_isolation()).
    Note: The isolate()/release() methods are kept for backward
    compatibility but they only update local state - hardware isolation
    is controlled separately via PRSystem.set_isolation(). # TODO: remove these
    """

    def __init__(self, partition_name: str):
        """
        Initialize partition boundary.

        Parameters
        ----------
        partition_name : str
            Name of the partition this boundary belongs to
        """
        self.partition_name = partition_name
        self._isolated = False

    @property
    def is_isolated(self) -> bool:
        """True if boundary is marked as isolated."""
        return self._isolated

    def isolate(self):
        """
        Mark boundary as isolated.

        Note: This only updates local state. Hardware isolation is
        controlled via PRSystem.set_isolation() which writes to the
        static region's isolation control register.
        """
        self._isolated = True
        logger.debug(f"Partition '{self.partition_name}' boundary marked isolated")

    def release(self):
        """
        Mark boundary as released.

        Note: This only updates local state. Hardware isolation is
        controlled via PRSystem.set_isolation().
        """
        self._isolated = False
        logger.debug(f"Partition '{self.partition_name}' boundary marked released")

    def __repr__(self) -> str:
        state = "isolated" if self._isolated else "open"
        return f"<PartitionBoundary '{self.partition_name}' state={state}>"
