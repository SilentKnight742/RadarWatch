"""Domain-specific RadarWatch errors."""


class RadarWatchError(RuntimeError):
    """Base exception for actionable pipeline failures."""


class ConfigurationError(RadarWatchError):
    """Raised when configuration is internally inconsistent."""


class AcquisitionError(RadarWatchError):
    """Raised when a required source cannot be resolved or downloaded."""


class DataValidationError(RadarWatchError):
    """Raised when source or derived geospatial data fails validation."""


class DetectionError(RadarWatchError):
    """Raised when flood evidence cannot be derived."""


class ImpactAnalysisError(RadarWatchError):
    """Raised when infrastructure analysis would produce a misleading result."""
