"""Read-only, per-process fail-closed usage watch; never a billing authority."""

import asyncio
import json
import math
import re

from scripts.performance.http import validate_connection
from scripts.performance.report import summarize_interval


class UsageUnknown(ValueError):
    pass


class UsageWatch:
    """Observe a fixed approved membership, not a randomly routed Service.

    Membership changes require a new explicitly bounded window. A disappearing
    instance is missing evidence, never an implicit zero or permission to retire.
    """

    def __init__(self, client, targets, *, profile_hash):
        if not isinstance(profile_hash, str) or not re.fullmatch(
            "[a-f0-9]{64}", profile_hash
        ):
            raise ValueError("Invalid usage profile")
        if not isinstance(targets, list) or not 1 <= len(targets) <= 16:
            raise ValueError("Explicit bounded usage targets required")
        urls, ids = set(), set()
        for target in targets:
            if not isinstance(target, dict) or set(target) != {
                "base_url",
                "instance_id",
                "config_hash",
            }:
                raise ValueError("Explicit usage identity required")
            validate_connection(
                target["base_url"],
                {"reader": "shape-only"},
                timeout=10,
                max_bytes=8 * 1024**2,
                max_calls=100000,
            )
            if any(not isinstance(target[k], str) or not target[k] for k in target):
                raise ValueError("Invalid usage identity")
            if (
                not re.fullmatch("[a-f0-9]{64}", target["config_hash"])
                or len(target["instance_id"]) > 128
            ):
                raise ValueError("Invalid usage identity")
            url = target["base_url"].rstrip("/")
            if url in urls or target["instance_id"] in ids:
                raise ValueError("Duplicate usage target")
            urls.add(url)
            ids.add(target["instance_id"])
        self.client, self.targets, self.profile_hash = (
            client,
            tuple(dict(t) for t in targets),
            profile_hash,
        )
        self.previous = {}
        self.samples = 0
        self.calls = 0
        self.error_class = None
        self.verified = False

    async def _json(self, url):
        self.calls += 1
        if self.calls > 100000:
            raise ValueError("Usage observation budget exhausted")

        async def read():
            async with self.client.stream("GET", url) as response:
                response.raise_for_status()
                if (
                    response.headers.get("content-type", "").split(";", 1)[0]
                    != "application/json"
                ):
                    raise ValueError("Usage JSON required")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > 8 * 1024**2:
                        raise ValueError("Usage response bound exceeded")
                    body.extend(chunk)
                return json.loads(body)

        return await asyncio.wait_for(read(), timeout=10)

    async def check(self, *, initial=False, final=False):
        tasks = [
            asyncio.create_task(
                self._check_target(target, initial=initial, final=final)
            )
            for target in self.targets
        ]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
        except BaseException:
            for task in tasks:
                task.cancel()
            joined = asyncio.gather(*tasks, return_exceptions=True)
            while not joined.done():
                try:
                    await asyncio.shield(joined)
                except asyncio.CancelledError:
                    continue
            joined.result()
            raise
        self.samples += 1
        self.verified = final

    async def _check_target(self, target, *, initial, final):
        url, uid = target["base_url"].rstrip("/"), target["instance_id"]
        who = await self._json(url + "/release/status")
        if any(
            who.get(k) != value
            for k, value in {
                "instance_id": uid,
                "config_hash": target["config_hash"],
                "profile_hash": self.profile_hash,
            }.items()
        ):
            raise ValueError("Usage instance identity differs")
        snapshot = (await self._json(url + "/health")).get("query_performance", {})
        if (
            snapshot.get("measurement_version") != 2
            or snapshot.get("enabled") is not True
        ):
            raise ValueError("Usage measurement unavailable")
        for field in ("active_queries", "observation_errors"):
            if type(snapshot.get(field)) is not int or snapshot[field] < 0:
                raise ValueError("Invalid usage observation")
        if snapshot["observation_errors"]:
            raise ValueError("Usage observation error")
        counters = snapshot.get("counters")
        if not isinstance(counters, list) or any(
            type(r.get("count")) is not int or r["count"] < 0 for r in counters
        ):
            raise ValueError("Invalid usage counter")
        # This also validates histogram vocabulary, buckets and epoch/reset.
        summarize_interval(self.previous.get(uid, snapshot), snapshot)
        groups = {}
        for row in counters:
            if row["name"] in {
                "provider_calls",
                "usage_reports",
                "usage_pending",
                "usage_unknown",
            }:
                key = (row["kind"], row["mode"], row["category"])
                groups.setdefault(key, {})[row["name"]] = row["count"]
        for group in groups.values():
            if not {"provider_calls", "usage_pending", "usage_unknown"} <= set(
                group
            ) or group["provider_calls"] != (
                group["usage_pending"]
                + group["usage_unknown"]
                + group.get("usage_reports", 0)
            ):
                raise ValueError("Incomplete usage lifecycle counters")
        if sum(r["count"] for r in counters if r["name"] == "usage_unknown"):
            raise UsageUnknown("Terminal usage missing; stop new work")
        if (initial or final) and (
            snapshot["active_queries"]
            or sum(r["count"] for r in counters if r["name"] == "usage_pending")
        ):
            raise ValueError("Usage window unsettled")
        self.previous[uid] = snapshot

    async def watch(self, stop, done, *, interval):
        if (
            type(interval) not in (int, float)
            or not math.isfinite(interval)
            or not 0.01 <= interval <= 60
        ):
            raise ValueError("Bounded usage sampling interval required")
        while not done.is_set():
            try:
                await asyncio.wait_for(done.wait(), timeout=interval)
            except TimeoutError:
                try:
                    await self.check()
                except Exception as error:
                    self.error_class = type(error).__name__
                    stop.set()
                    return

    def report(self):
        return dict(
            verified=self.verified and self.error_class is None,
            error_class=self.error_class,
            samples=self.samples,
            instance_count=len(self.targets),
            calls=self.calls,
        )
