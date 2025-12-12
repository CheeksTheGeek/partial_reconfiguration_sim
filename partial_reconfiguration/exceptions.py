"""Custom exceptions for the PR simulation system."""


class PRError(Exception):
    """Base exception for all PR-related errors."""
    pass


class PRConfigError(PRError):
    """
    Configuration error.

    Raised when:
    - Configuration file not found or unreadable
    - Invalid YAML/JSON/TOML syntax
    - Missing required fields
    - Invalid field values
    - Duplicate partition or RM names
    - References to non-existent partitions
    """
    pass


class PRValidationError(PRError):
    """
    Port compatibility validation error.

    Raised when:
    - Port type mismatch between partition and RM
    - Port direction mismatch
    - Port width incompatible (depending on policy)
    - Missing port mapping for required partition port
    - RM has extra ports not allowed by policy
    """
    pass


class PRReconfigurationError(PRError):
    """
    Reconfiguration error.

    Raised when:
    - Attempting to reconfigure with unregistered RM
    - RM not compatible with target partition
    - Process termination failure
    - New process startup failure
    - Timeout waiting for idle state
    """
    pass


class PRBuildError(PRError):
    """
    Build error.

    Raised when:
    - Verilator/Icarus compilation fails
    - Source files not found
    - Invalid module parameters
    """
    pass
