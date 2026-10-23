"""An explicit release profile; the default has no new runtime dependencies."""

from dataclasses import dataclass
import os
import re


@dataclass(frozen=True)
class ReleaseConfig:
    scope: str
    slot: str
    instance_id: str
    config_hash: str
    profile_hash: str
    protocol_version: int = 1
    usage_final_path: str | None = None

    @classmethod
    def from_env(cls, *, distributed_writes=None):
        flag = os.getenv("LIGHTRAG_RELEASE_ENABLED", "false")
        if flag not in {"true", "false"}:
            raise ValueError("LIGHTRAG_RELEASE_ENABLED must be true or false")
        if flag == "false":
            return None
        if distributed_writes is None:
            distributed_writes = os.getenv("LIGHTRAG_DISTRIBUTED_WRITES") == "true"
        if distributed_writes is not True or os.getenv("WORKERS") != "1":
            raise ValueError("Release requires a distributed single-worker profile")
        values = {
            name: os.getenv(name, "")
            for name in (
                "LIGHTRAG_DEPLOYMENT_ID",
                "WORKSPACE",
                "LIGHTRAG_RELEASE_SLOT",
                "LIGHTRAG_RELEASE_INSTANCE_ID",
                "LIGHTRAG_RELEASE_CONFIG_HASH",
                "LIGHTRAG_RELEASE_PROFILE_HASH",
            )
        }
        for key in (
            "LIGHTRAG_DEPLOYMENT_ID",
            "WORKSPACE",
            "LIGHTRAG_RELEASE_INSTANCE_ID",
        ):
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", values[key]):
                raise ValueError(f"{key} is required and must be an identifier")
        if values["LIGHTRAG_RELEASE_SLOT"] not in {"blue", "green"}:
            raise ValueError("LIGHTRAG_RELEASE_SLOT must be blue or green")
        for key in ("LIGHTRAG_RELEASE_CONFIG_HASH", "LIGHTRAG_RELEASE_PROFILE_HASH"):
            if not re.fullmatch(r"[a-f0-9]{64}", values[key]):
                raise ValueError(f"{key} must be an explicit SHA256")
        final_path = os.getenv("LIGHTRAG_USAGE_FINAL_PATH", "")
        if final_path not in {"", "/dev/termination-log"}:
            raise ValueError(
                "Only the explicit native termination usage path is supported"
            )
        return cls(
            values["LIGHTRAG_DEPLOYMENT_ID"] + "/" + values["WORKSPACE"],
            values["LIGHTRAG_RELEASE_SLOT"],
            values["LIGHTRAG_RELEASE_INSTANCE_ID"],
            values["LIGHTRAG_RELEASE_CONFIG_HASH"],
            values["LIGHTRAG_RELEASE_PROFILE_HASH"],
            usage_final_path=final_path or None,
        )
