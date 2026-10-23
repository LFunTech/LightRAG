"""Explicit first-release risk acceptance; never a general privilege bypass."""

from dataclasses import asdict, dataclass
import json
import re

from .profile import physical_profile_hash

ENV_KEY = "LIGHTRAG_FIRST_RELEASE_CREDENTIAL_EXCEPTION"
ONBOARDING_KEY = "first_release_credential_exception"


@dataclass(frozen=True)
class CredentialException:
    version: int
    approval_id: str
    scope: str
    profile_hash: str
    release_tag: str
    postgres_user: str
    postgres_database: str
    hugegraph_user: str
    accepted_risk: bool

    def as_dict(self):
        return asdict(self)

    @classmethod
    def parse(cls, value, *, scope, profile_hash):
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("Complete first-release credential approval required")
        if (
            type(value["version"]) is not int
            or value["version"] != 1
            or value["accepted_risk"] is not True
            or value["scope"] != scope
            or value["profile_hash"] != profile_hash
        ):
            raise ValueError("First-release credential approval differs")
        patterns = {
            "approval_id": r"[A-Za-z0-9_.-]{1,128}",
            "scope": r"[A-Za-z0-9_.-]{1,128}/[A-Za-z0-9_.-]{1,128}",
            "profile_hash": r"[a-f0-9]{64}",
            "release_tag": r"v[0-9]+\.[0-9]+\.[0-9]+",
            "postgres_user": r"[a-z][a-z0-9_]{0,62}",
        }
        for key, pattern in patterns.items():
            if not isinstance(value[key], str) or not re.fullmatch(pattern, value[key]):
                raise ValueError("Invalid first-release approval field: " + key)
        if re.search(r"(^|[/_.-])test([/_.-]|$)", value["scope"], re.I):
            raise ValueError("First-release exception cannot target test")
        for key in ("postgres_database", "hugegraph_user"):
            text = value[key]
            if (
                not isinstance(text, str)
                or not text
                or len(text) > 128
                or any(ord(char) < 32 or ord(char) == 127 for char in text)
            ):
                raise ValueError("Invalid first-release approval field: " + key)
        return cls(**value)

    async def verify_principal(self, connection, role):
        if role != self.postgres_user:
            raise ValueError("First-release PostgreSQL principal differs")
        row = await connection.fetchrow(
            """
            SELECT current_user AS principal, session_user AS session_principal,
              current_database() AS database,
              EXISTS(SELECT FROM pg_roles r WHERE
                (r.rolsuper OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication OR r.rolbypassrls)
                AND pg_has_role(current_user,r.oid,'MEMBER')) AS privileged
            """
        )
        if (
            not row
            or row["principal"] != self.postgres_user
            or row.get("session_principal") != self.postgres_user
            or row["database"] != self.postgres_database
            or row["privileged"] is not False
        ):
            raise ValueError(
                "First-release database identity or privilege audit failed"
            )

    async def verify_initialization_state(self, connection):
        if not await connection.fetchval(
            "SELECT to_regclass('lightrag_release.scopes') IS NOT NULL"
        ):
            return
        row = await connection.fetchrow(
            "SELECT scope,profile_hash,active_slot,epoch,phase,owner,gate_open,policy "
            "FROM lightrag_release.scopes WHERE scope=$1",
            self.scope,
        )
        if row is not None:
            state = dict(row)
            if isinstance(state.get("policy"), str):
                state["policy"] = json.loads(state["policy"])
            self.verify_scope_state(state)

    def verify_scope_state(self, state):
        if (
            state.get("policy", {}).get(ONBOARDING_KEY) != self.as_dict()
            or state.get("scope") != self.scope
            or state.get("profile_hash") != self.profile_hash
            or state.get("active_slot") != "blue"
            or state.get("epoch") != 1
            or state.get("phase") != "IDLE"
            or state.get("owner") is not None
            or state.get("gate_open") is not True
        ):
            raise ValueError(
                "First-release credential approval or baseline state differs"
            )


def from_environment(environment):
    if ENV_KEY not in environment:
        return None
    raw = environment[ENV_KEY]
    if not isinstance(raw, str) or len(raw.encode()) > 4096:
        raise ValueError("Bounded first-release credential approval required")
    try:
        value = json.loads(raw)
    except ValueError:
        raise ValueError("Invalid first-release credential approval JSON") from None
    policy = CredentialException.parse(
        value,
        scope=environment.get("LIGHTRAG_DEPLOYMENT_ID", "")
        + "/"
        + environment.get("WORKSPACE", ""),
        profile_hash=physical_profile_hash(environment),
    )
    if any(
        environment.get(key) != wanted
        for key, wanted in {
            "POSTGRES_USER": policy.postgres_user,
            "POSTGRES_DATABASE": policy.postgres_database,
            "HUGEGRAPH_USERNAME": policy.hugegraph_user,
            "LIGHTRAG_RELEASE_TAG": policy.release_tag,
        }.items()
    ):
        raise ValueError("First-release credentials or release tag differ")
    return policy


def for_onboarding(config):
    value = config.onboarding.get(ONBOARDING_KEY)
    if ONBOARDING_KEY not in config.onboarding:
        return None
    policy = CredentialException.parse(
        value,
        scope=config.target["deployment_id"] + "/" + config.target["workspace"],
        profile_hash=config.onboarding["profile_hash"],
    )
    if (
        policy.postgres_user != config.onboarding["runtime_role"]
        or policy.postgres_database != config.target["postgres_database"]
    ):
        raise ValueError("First-release onboarding principal or database differs")
    return policy


def validate_slot(config, identity, slot, environment):
    policy = for_onboarding(config)
    if policy is None:
        if ENV_KEY in environment:
            raise ValueError("Unapproved first-release credential exception")
        return
    try:
        supplied = json.loads(environment.get(ENV_KEY, ""))
    except ValueError:
        supplied = None
    if (
        slot != "blue"
        or identity.get("tag") != policy.release_tag
        or environment.get("LIGHTRAG_RELEASE_TAG") != policy.release_tag
        or supplied != policy.as_dict()
    ):
        raise ValueError("Credential exception is limited to the pinned first Blue")
