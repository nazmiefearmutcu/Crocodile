"""The Crocodile exception hierarchy.

One root so callers can catch everything this library raises, and a shallow
tree beneath it so they rarely have to.

This module imports nothing from the rest of the package, so any module may
depend on it without risking a cycle.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "BookGap",
    "CapabilityUnavailable",
    "CaptureError",
    "CaptureIntegrityError",
    "ConfigError",
    "ConnectorError",
    "CrocodileError",
    "FatalConnectorError",
    "ProvenanceError",
    "SinkError",
    "StoreError",
    "TransientConnectorError",
]


class CrocodileError(Exception):
    """Root of every error this library raises."""


class ConnectorError(CrocodileError):
    """A source-side failure."""


class FatalConnectorError(ConnectorError):
    """The source rejected us and retrying cannot help.

    Bad credentials, an unsupported channel, a symbol the venue does not list.
    Do not reconnect.
    """


class TransientConnectorError(ConnectorError):
    """Recoverable — reconnecting with backoff is appropriate."""


class SinkError(CrocodileError):
    """Writing to the output sink failed. Not a bad market-data frame."""


class StoreError(CrocodileError):
    """The lake or its catalog could not satisfy the request."""


class CaptureError(SinkError):
    """Required raw capture failed; never silently continue or reconnect."""


class CaptureIntegrityError(StoreError):
    """Raw capture bytes, references, or manifest are inconsistent."""


class BookGap(StoreError):
    """An order-book sequence discontinuity was detected during reconstruction."""


class ConfigError(CrocodileError):
    """Crocodile's own configuration is missing, malformed, or self-contradictory.

    Detected before any source is contacted. A credential the venue rejects is a
    :class:`FatalConnectorError`, not this.
    """


class ProvenanceError(CrocodileError):
    """A record's provenance could not be established."""


def _rebuild_capability_unavailable(
    capability: str, asset_class: str, reason: str
) -> CapabilityUnavailable:
    """Reconstruct a :class:`CapabilityUnavailable` from its three fields.

    Module scope, not a ``staticmethod``: pickle stores this callable by qualified name and
    imports it back, so it has to be findable as ``crocodile.core.errors._rebuild_...``.
    """
    return CapabilityUnavailable(capability, asset_class, reason=reason)


class CapabilityUnavailable(CrocodileError):
    """A capability has no implementation for this asset class in this deployment.

    This is the raised form of ``Provenance.UNAVAILABLE``. Capabilities that can
    return typed holes should prefer emitting ``UNAVAILABLE`` records; this is for
    the cases where there is nothing to return at all.
    """

    def __init__(self, capability: str, asset_class: str, *, reason: str) -> None:
        self.capability = capability
        # str, not the AssetClass enum: importing it here would close a cycle, since
        # capability.py imports provenance.py, which imports this module.
        self.asset_class = asset_class
        self.reason = reason
        super().__init__(f"capability {capability!r} unavailable for {asset_class}: {reason}")

    def __reduce__(self) -> tuple[Any, ...]:
        """Rebuild from the three fields rather than from ``args``.

        ``BaseException.__reduce__`` returns ``(cls, self.args, self.__dict__)``, and
        ``args`` is the single formatted message this class handed to ``super().__init__``.
        Reconstruction would therefore call the two-positional-argument constructor with one
        argument and die with ``TypeError``. That ``TypeError`` is raised *during unpickling*,
        so it replaces the error being transported and surfaces far from its origin — the
        loudest possible failure in the one class that exists to cross a process or wire
        boundary. This also covers ``copy`` and ``deepcopy``, which go through ``__reduce__``.
        """
        return (
            _rebuild_capability_unavailable,
            (self.capability, self.asset_class, self.reason),
        )
