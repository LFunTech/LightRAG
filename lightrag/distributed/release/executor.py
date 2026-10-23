"""Execute an admitted command once; unknown outcomes are never retry hints."""

import asyncio
import inspect
from dataclasses import dataclass

from .commands import canonical


@dataclass(frozen=True)
class CommandOutcome:
    status: str
    result: object


async def execute_command(registry, envelope, *, authorize):
    try:
        authorized = await authorize(
            envelope.principal, envelope.operation, envelope.scope
        )
    except Exception:
        return {"status": "FAILED", "result": {"error": "AuthorizationUnavailable"}}
    if not authorized:
        return {"status": "FAILED", "result": {"error": "AuthorizationDenied"}}
    try:
        result = registry.handler(envelope)(envelope.payload)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, CommandOutcome):
            if result.status not in {"SUCCEEDED", "FAILED", "UNCERTAIN"}:
                raise ValueError("Invalid command outcome")
            canonical(result.result)
            return {"status": result.status, "result": result.result}
        canonical(result)
        return {"status": "SUCCEEDED", "result": result}
    except asyncio.CancelledError:
        raise
    except Exception as error:
        return {"status": "UNCERTAIN", "result": {"error": type(error).__name__}}
