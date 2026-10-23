"""Versioned, credential-free release identities and phase checks."""

from dataclasses import asdict, dataclass
import re

from lightrag.distributed.coordinator import CoordinationError


class ReleaseError(CoordinationError):
    """Release control failed closed; no automatic recovery is authorized."""


class ReleaseConflict(ReleaseError):
    """Owner, epoch or release identity does not match."""


class ReleaseBudgetExceeded(ReleaseConflict):
    """A known pre-transfer drain budget expired; safe abort must be observed."""


class ReleaseDraining(ReleaseConflict):
    """Known in-flight work is still finishing; no ownership mutation happened."""


class ReleaseSchemaError(ReleaseError):
    """The separately provisioned release schema is missing or drifted."""


class ReleaseUnavailable(ReleaseError):
    """Release state is unknown; never treat it as an open gate or empty queue."""


class QueueFull(ReleaseError):
    """Capacity was exhausted before accepting an execution obligation."""


PHASES = (
    "PREPARED",
    "DRAINING",
    "READ_SWITCHED",
    "WRITER_SWITCHED",
    "RETIRING",
    "COMPLETED",
)


@dataclass(frozen=True)
class ReleaseIdentity:
    scope: str
    release_id: str
    tag: str
    commit: str
    digest: str
    config_hash: str
    protocol_version: int = 1

    def __post_init__(self):
        patterns = {
            "scope": r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
            "release_id": r"[A-Za-z0-9_.-]{1,128}",
            "tag": r"v[0-9]+\.[0-9]+\.[0-9]+",
            "commit": r"[a-f0-9]{40}",
            "digest": r"sha256:[a-f0-9]{64}",
            "config_hash": r"[a-f0-9]{64}",
        }
        if (
            any(
                not isinstance(getattr(self, key), str)
                or not re.fullmatch(pattern, getattr(self, key))
                for key, pattern in patterns.items()
            )
            or self.protocol_version != 1
        ):
            raise ValueError("Invalid release identity")

    def as_dict(self):
        return asdict(self)


def validate_transition(row, identity, *, expected_epoch, next_phase):
    if (
        row["owner"] != identity.release_id
        or row["epoch"] != expected_epoch
        or row["identity"] != identity.as_dict()
    ):
        raise ReleaseConflict("Release owner, epoch or identity mismatch")
    phase = row["phase"]
    if (
        phase not in PHASES
        or phase == "COMPLETED"
        or next_phase != PHASES[PHASES.index(phase) + 1]
    ):
        raise ReleaseConflict("Release phase transition refused")
    return next_phase
