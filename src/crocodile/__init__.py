"""Crocodile — deterministic market-data engine for crypto and US equities."""

from crocodile.core.capability import AssetClass, Capability, ReturnKind
from crocodile.core.config import Settings
from crocodile.core.errors import (
    CapabilityUnavailable,
    ConfigError,
    ConnectorError,
    CrocodileError,
    SinkError,
    StoreError,
)
from crocodile.core.schema.provenance import Provenance
from crocodile.core.schema.records import Record
from crocodile.core.store.catalog import Catalog

__version__ = "0.3.1"

__all__ = [
    "AssetClass",
    "Capability",
    "CapabilityUnavailable",
    "Catalog",
    "ConfigError",
    "ConnectorError",
    "CrocodileError",
    "Provenance",
    "Record",
    "ReturnKind",
    "Settings",
    "SinkError",
    "StoreError",
    "__version__",
]
