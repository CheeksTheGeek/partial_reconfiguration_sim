from enum import Enum, auto
from dataclasses import dataclass
from typing import Optional, Dict, Any
import time
import logging

logger = logging.getLogger(__name__)


class ConfigInterface(Enum):
    """
    Configuration interface types with their throughput characteristics.
    """

    ICAP_XILINX = auto()
    """
    Internal Configuration Access Port (Xilinx).
    Typical: 32-bit @ 100-200 MHz = 400-800 MB/s.
    """

    PCAP_ZYNQ = auto()
    """
    Processor Configuration Access Port (Zynq).
    DMA from processor memory, ~400 MB/s typical.
    """

    PR_CONTROLLER_INTEL = auto()
    """
    Intel PR Controller IP.
    Varies by implementation, typically 100-400 MB/s.
    """

    INSTANT = auto()
    """
    Instant configuration (no delay).
    For fast simulation iteration.
    """


@dataclass
class ConfigInterfaceSpec:
    """Specification for a configuration interface."""

    interface: ConfigInterface
    throughput_mbps: float  # Megabytes per second
    overhead_ms: float = 0.5  # Fixed overhead per reconfiguration

INTERFACE_SPECS: Dict[ConfigInterface, ConfigInterfaceSpec] = {
    ConfigInterface.ICAP_XILINX: ConfigInterfaceSpec(
        interface=ConfigInterface.ICAP_XILINX,
        throughput_mbps=400.0,  # Conservative estimate
        overhead_ms=0.5
    ),
    ConfigInterface.PCAP_ZYNQ: ConfigInterfaceSpec(
        interface=ConfigInterface.PCAP_ZYNQ,
        throughput_mbps=200.0,  # Processor-limited
        overhead_ms=1.0
    ),
    ConfigInterface.PR_CONTROLLER_INTEL: ConfigInterfaceSpec(
        interface=ConfigInterface.PR_CONTROLLER_INTEL,
        throughput_mbps=200.0,
        overhead_ms=0.5
    ),
    ConfigInterface.INSTANT: ConfigInterfaceSpec(
        interface=ConfigInterface.INSTANT,
        throughput_mbps=float('inf'),
        overhead_ms=0.0
    ),
}


@dataclass
class BitstreamModel:
    """
    Model for RM bitstream characteristics.

    In real designs, bitstream size depends on:
    - RM region size (number of CLBs, BRAMs, DSPs)
    - Compression (Xilinx supports bitstream compression)
    - Frame-based addressing overhead

    For simulation, we use explicit config_time_ms from the RM
    configuration, or estimate from approximate size.
    """

    rm_name: str
    size_bytes: Optional[int] = None  # Explicit size if known
    config_time_ms: Optional[float] = None  # Explicit time if specified
    clb_count: int = 0
    bram_count: int = 0
    dsp_count: int = 0
    BYTES_PER_CLB = 100
    BYTES_PER_BRAM = 5000
    BYTES_PER_DSP = 500

    def estimate_size_bytes(self) -> int:
        """Estimate bitstream size from resources."""
        if self.size_bytes is not None:
            return self.size_bytes
        estimated = (
            self.clb_count * self.BYTES_PER_CLB +
            self.bram_count * self.BYTES_PER_BRAM +
            self.dsp_count * self.BYTES_PER_DSP
        )
        return max(estimated, 1000)


class ConfigurationTimingModel:
    """
    Models configuration timing for PR simulation.

    This class calculates and applies configuration delays based on:
    1. RM-specified config_time_ms (preferred, explicit)
    2. Estimated from bitstream size and interface throughput
    3. Zero delay when timing is disabled (default)

    Usage:
        model = ConfigurationTimingModel(enabled=True)
        delay = model.get_config_time_ms('my_rm', config_time_ms=50.0)
        model.apply_delay(delay)
    """

    def __init__(
        self,
        enabled: bool = False,
        interface: ConfigInterface = ConfigInterface.ICAP_XILINX,
        custom_spec: ConfigInterfaceSpec = None
    ):
        """
        Initialize timing model.

        Parameters
        ----------
        enabled : bool
            If False, all delays are zero (instant swap).
            If True, RM must specify config_time_ms.
        interface : ConfigInterface
            Default configuration interface type
        custom_spec : ConfigInterfaceSpec
            Custom interface specification (overrides interface)
        """
        self.enabled = enabled
        self.interface = interface

        if custom_spec is not None:
            self.spec = custom_spec
        else:
            self.spec = INTERFACE_SPECS.get(interface, INTERFACE_SPECS[ConfigInterface.INSTANT])
        self.total_reconfigurations = 0
        self.total_config_time_ms = 0.0

    def get_config_time_ms(
        self,
        rm_name: str,
        config_time_ms: Optional[float] = None,
        bitstream: Optional[BitstreamModel] = None
    ) -> float:
        """
        Get configuration time for an RM.

        Parameters
        ----------
        rm_name : str
            Name of the RM (for logging)
        config_time_ms : float, optional
            Explicit configuration time from RM config
        bitstream : BitstreamModel, optional
            Bitstream model for size-based estimation

        Returns
        -------
        float
            Configuration time in milliseconds (0 if timing disabled)
        """
        if not self.enabled:
            return 0.0
        if config_time_ms is not None:
            return config_time_ms
        if bitstream is not None:
            size_bytes = bitstream.estimate_size_bytes()
            if bitstream.config_time_ms is not None:
                return bitstream.config_time_ms
            size_mb = size_bytes / (1024 * 1024)
            transfer_time_ms = (size_mb / self.spec.throughput_mbps) * 1000

            return transfer_time_ms + self.spec.overhead_ms
        logger.warning(
            f"RM '{rm_name}': config_timing enabled but no config_time_ms specified. "
            f"Using instant swap. Add 'config_time_ms: <value>' to RM config."
        )
        return 0.0

    def apply_delay(self, delay_ms: float):
        """
        Apply configuration delay.

        This simulates the time spent loading the bitstream.
        During this time, the partition is isolated.

        Parameters
        ----------
        delay_ms : float
            Delay in milliseconds
        """
        if delay_ms > 0:
            logger.debug(f"Applying configuration delay: {delay_ms:.2f} ms")
            time.sleep(delay_ms / 1000.0)
            self.total_reconfigurations += 1
            self.total_config_time_ms += delay_ms

    def get_stats(self) -> Dict[str, Any]:
        """Get timing statistics."""
        avg_time = 0.0
        if self.total_reconfigurations > 0:
            avg_time = self.total_config_time_ms / self.total_reconfigurations

        return {
            'enabled': self.enabled,
            'interface': self.interface.name,
            'total_reconfigurations': self.total_reconfigurations,
            'total_config_time_ms': self.total_config_time_ms,
            'avg_config_time_ms': avg_time
        }

    def reset_stats(self):
        """Reset timing statistics."""
        self.total_reconfigurations = 0
        self.total_config_time_ms = 0.0

    def __repr__(self) -> str:
        status = "enabled" if self.enabled else "disabled"
        return f"<ConfigurationTimingModel {status} interface={self.interface.name}>"
