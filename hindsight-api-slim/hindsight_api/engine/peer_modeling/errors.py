"""Errors raised by the peer-modeling service and mapped by the HTTP layer."""


class PeerModelingError(Exception):
    """Base class for deterministic peer-modeling failures."""


class PeerNotFoundError(PeerModelingError):
    """A bank, peer, or directional model does not exist."""


class PeerConflictError(PeerModelingError):
    """A peer identity conflicts with an existing bank-scoped identity."""


class PeerValidationError(PeerModelingError):
    """A supplied claim or directional operation is invalid."""


class PeerFeatureDisabledError(PeerModelingError):
    """Peer-model reads/writes are disabled for this bank or deployment."""


class PeerModelingUnavailableError(PeerModelingError):
    """The later LLM-backed peer-modeling worker is not installed yet."""
