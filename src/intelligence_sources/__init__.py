"""Public-safe adapters for GateX Intelligence source intake."""

from .contract import INTAKE_SCHEMA, build_envelope, envelopes_from_batch

__all__ = ["INTAKE_SCHEMA", "build_envelope", "envelopes_from_batch"]
