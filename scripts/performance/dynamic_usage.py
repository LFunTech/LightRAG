"""Per-process 4/8/4 usage ledger, retirement gate and cooperative stop owner."""

import asyncio
import copy
import hashlib
import json
import math
import os
import time
from pathlib import Path

from lightrag.distributed.release.usage import FIELDS, UsageUnknown, validate_usage
from scripts.performance.http import save_json

PHASES = (
    "PREPARED",
    "DRAINING",
    "READ_SWITCHED",
    "WRITER_SWITCHED",
    "RETIRING",
    "COMPLETED",
)


def validate_limits(limits):
    required = {
        "poll_seconds",
        "sample_timeout_seconds",
        "transition_timeout_seconds",
        "max_samples",
        "max_evidence_bytes",
    }
    if not isinstance(limits, dict) or set(limits) != required:
        raise ValueError("Explicit dynamic usage budgets required")
    for key in required - {"max_samples", "max_evidence_bytes"}:
        value = limits[key]
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not 0 < value <= 600
        ):
            raise ValueError("Approved finite usage timing budget required")
    if limits["poll_seconds"] >= min(
        limits["sample_timeout_seconds"], limits["transition_timeout_seconds"]
    ):
        raise ValueError("Usage poll budget exceeds deadline")
    for key, cap in [("max_samples", 100000), ("max_evidence_bytes", 16 * 1024**2)]:
        if type(limits[key]) is not int or not 1 <= limits[key] <= cap:
            raise ValueError("Approved bounded usage evidence budget required")


class DynamicUsage:
    def __init__(self, control, settings, limits, output_dir, stop):
        validate_limits(limits)
        self.control, self.settings, self.limits = control, settings, dict(limits)
        self.output_dir, self.stop = Path(output_dir), stop
        self.members, self.baselines, self.waiting = {}, {}, {}
        self.retired, self.saved = {}, {}
        self.samples = self.bytes = 0
        self.started = self.verified = False
        self.error_class = None
        self.phase = None
        self.lock = asyncio.Lock()
        self.done = asyncio.Event()
        self.task = None

    def ensure_running(self):
        if self.stop.is_set() or self.error_class is not None:
            raise ValueError("Dynamic usage stopped; no new work or scaling")

    async def start(self):
        await self.sample(initial=True)
        self.started = True

    def launch(self):
        if not self.started or self.task is not None:
            raise ValueError("Usage baseline must precede observation")
        self.task = asyncio.create_task(self.watch())

    async def watch(self):
        while not self.done.is_set():
            try:
                await asyncio.wait_for(self.done.wait(), self.limits["poll_seconds"])
            except TimeoutError:
                try:
                    await self.sample()
                except Exception:
                    return

    async def _persist(self, function, *args):
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        failure = None
        try:
            done, _ = await asyncio.wait(
                {task}, timeout=max(0, self.deadline - time.monotonic())
            )
            if not done:
                failure = TimeoutError("Usage evidence deadline exceeded")
        except asyncio.CancelledError as error:
            failure = error
        if failure is not None:
            self.stop.set()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                self.stop.set()
                failure = failure or error
            except Exception:
                break
        if failure is not None:
            task.exception()
            raise failure
        return task.result()

    async def _append(self, record):
        content = (
            json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + "\n"
        ).encode()
        if self.bytes + len(content) > self.limits["max_evidence_bytes"]:
            raise ValueError("Usage evidence budget exhausted")
        await self._persist(self._append_content, content)
        self.bytes += len(content)

    def _append_content(self, content):
        fd = os.open(
            self.output_dir / "usage-observations.jsonl",
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
            0o600,
        )
        try:
            while content:
                n = os.write(fd, content)
                if n <= 0:
                    raise OSError("Usage evidence write failed")
                content = content[n:]
            os.fsync(fd)
        finally:
            os.close(fd)

    def _save_final(self, path, row):
        save_json(path, row)
        fd = os.open(self.output_dir, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return hashlib.sha256(path.read_bytes()).hexdigest()

    async def sample(self, *, initial=False, tail=False):
        async with self.lock:
            try:
                self.deadline = time.monotonic() + self.limits["sample_timeout_seconds"]
                if self.error_class and not tail:
                    raise ValueError("Usage observation has already failed")
                if self.samples >= self.limits["max_samples"]:
                    raise ValueError("Usage sample budget exhausted")
                self.samples += 1
                task = asyncio.create_task(self.control.usage(self.settings))
                try:
                    done, _ = await asyncio.wait(
                        {task}, timeout=self.limits["sample_timeout_seconds"]
                    )
                    if not done:
                        raise TimeoutError("Usage sample deadline exceeded")
                    frame = task.result()
                except BaseException:
                    # Stop admission before joining slow read-only cancellation
                    # cleanup. No observer task or underlying read is abandoned.
                    self.stop.set()
                    task.cancel()
                    joined = asyncio.gather(task, return_exceptions=True)
                    while not joined.done():
                        try:
                            await asyncio.shield(joined)
                        except asyncio.CancelledError:
                            continue
                    joined.result()
                    raise
                await self._observe(frame, initial=initial)
            except BaseException as error:
                self.error_class = self.error_class or type(error).__name__
                self.verified = False
                self.stop.set()
                raise

    async def _observe(self, frame, *, initial):
        unknown = False
        finalized = {}
        if not isinstance(frame, dict) or set(frame) != {"phase", "epoch", "members"}:
            raise ValueError("Complete bounded usage frame required")
        phase, epoch = frame["phase"], frame["epoch"]
        if type(epoch) is not int or epoch != self.settings["epoch"] + int(
            not initial and phase in PHASES[3:]
        ):
            raise ValueError("Usage release epoch changed unexpectedly")
        if not initial and (
            phase not in PHASES
            or self.phase in PHASES
            and PHASES.index(phase) < PHASES.index(self.phase)
        ):
            raise ValueError("Usage release phase regressed")
        items = frame["members"]
        if not isinstance(items, list) or len(items) > 8:
            raise ValueError("Bounded 4+4 membership required")
        current = {}
        for row in items:
            required = {"uid", "slot", "config_hash", "profile_hash", "digest", "state"}
            if not isinstance(row, dict) or set(row) != required | (
                {"usage"} if row.get("state") in {"live", "final"} else set()
            ):
                raise ValueError("Bounded usage member fields required")
            uid, slot = row["uid"], row["slot"]
            if (
                not isinstance(uid, str)
                or not 1 <= len(uid) <= 128
                or uid in current
                or slot not in {"blue", "green"}
            ):
                raise ValueError("Duplicate or invalid usage member")
            if any(
                row[k] != expected
                for k, expected in {
                    "config_hash": self.settings[slot + "_config_hash"],
                    "profile_hash": self.settings["profile_hash"],
                    "digest": self.settings[slot + "_digest"],
                }.items()
            ):
                raise ValueError("Usage member identity differs")
            current[uid] = row
        blue = {k for k, v in current.items() if v["slot"] == "blue"}
        green = {k for k, v in current.items() if v["slot"] == "green"}
        if initial:
            if len(blue) != 4 or len(green) not in {0, 4}:
                raise ValueError("Clean complete baseline membership required")
        elif (len(green) != 4 and phase != "PREPARED") or len(green) > 4:
            raise ValueError("Green membership is incomplete")
        for uid, old in self.members.items():
            if uid not in current and uid not in self.retired:
                raise ValueError("Usage member disappeared without final settlement")
            if uid in current and any(
                old[k] != current[uid][k]
                for k in ("slot", "config_hash", "profile_hash", "digest")
            ):
                raise ValueError("Usage member changed identity")
        # Detect terminal missing usage in the complete bounded frame before
        # any slow evidence I/O. Saving the failure must never delay stopping
        # new work, including when a later member carries the unknown count.
        for uid, row in current.items():
            if row["state"] in {"live", "final"}:
                validate_usage(
                    self.members.get(uid, {}).get("usage"),
                    row["usage"],
                    quiescent=initial or row["state"] == "final",
                    allow_unknown=True,
                )
                if any(v[2] for v in row["usage"]["counts"].values()):
                    self.stop.set()
        for uid, row in current.items():
            new = uid not in self.members
            if (
                not initial
                and new
                and (
                    row["slot"] != "green"
                    or phase not in {"PREPARED", "DRAINING"}
                    or len([m for m in self.members.values() if m["slot"] == "green"])
                    >= 4
                )
            ):
                raise ValueError("Unexpected replacement or joining usage member")
            state = row["state"]
            if state in {"joining", "closing"}:
                old_usage = self.members.get(uid, {}).get("usage")
                if state == "joining" and old_usage is not None:
                    raise ValueError("Observed process regressed to joining")
                if (
                    initial
                    or (
                        state == "joining"
                        and (row["slot"] != "green" or phase != "PREPARED")
                    )
                    or (
                        state == "closing"
                        and (row["slot"] != "blue" or phase != "RETIRING")
                    )
                ):
                    raise ValueError("Unexpected incomplete usage member")
                since = self.waiting.setdefault(uid, time.monotonic())
                if time.monotonic() - since > self.limits["transition_timeout_seconds"]:
                    raise ValueError("Final or joining usage evidence timed out")
                self.members[uid] = copy.deepcopy(row)
                if old_usage is not None:
                    self.members[uid]["usage"] = old_usage
                continue
            if state not in {"live", "final"}:
                raise ValueError("Unknown usage member state")
            value = row["usage"]
            old_usage = self.members.get(uid, {}).get("usage")
            validate_usage(
                old_usage,
                value,
                quiescent=initial or state == "final",
                allow_unknown=True,
            )
            if uid in self.retired and row != self.retired[uid]:
                raise ValueError("Sealed final usage changed")
            if state == "final":
                if row["slot"] != "blue" or phase not in {"RETIRING", "COMPLETED"}:
                    raise ValueError("Unexpected instance finalization")
                if uid not in self.saved:
                    path = self.output_dir / (
                        "usage-final-"
                        + hashlib.sha256(uid.encode()).hexdigest()
                        + ".json"
                    )
                    size = len(
                        (
                            json.dumps(
                                row, ensure_ascii=False, indent=2, allow_nan=False
                            )
                            + "\n"
                        ).encode()
                    )
                    if self.bytes + size > self.limits["max_evidence_bytes"]:
                        raise ValueError("Usage evidence budget exhausted")
                    digest = await self._persist(self._save_final, path, row)
                    self.bytes += size
                    self.saved[uid] = digest
            known = True
            try:
                validate_usage(old_usage, value, quiescent=initial or state == "final")
            except UsageUnknown:
                unknown, known = True, False
            if new:
                self.baselines[uid] = (
                    copy.deepcopy(value)
                    if initial
                    else dict(value, counts={}, uptime_seconds=0)
                )
            elif uid not in self.baselines:
                # A newly created process first observed while joining contributes
                # its complete counters from process start, not a guessed first delta.
                self.baselines[uid] = dict(value, counts={}, uptime_seconds=0)
            self.waiting.pop(uid, None)
            self.members[uid] = copy.deepcopy(row)
            if state == "final" and known:
                finalized[uid] = copy.deepcopy(row)
        await self._append(
            dict(
                phase=phase,
                epoch=epoch,
                initial=initial,
                members=[
                    {
                        k: v
                        for k, v in row.items()
                        if k
                        in {
                            "uid",
                            "slot",
                            "config_hash",
                            "profile_hash",
                            "digest",
                            "state",
                            "usage",
                        }
                    }
                    for row in current.values()
                ],
            )
        )
        self.retired.update(finalized)
        self.phase = None if initial else phase
        if unknown:
            raise UsageUnknown("Terminal usage missing; observed counts retained")

    async def before_scale(self):
        deadline = time.monotonic() + self.limits["transition_timeout_seconds"]
        while True:
            self.ensure_running()
            await self.sample()
            self.ensure_running()
            if self.phase == "RETIRING" and len(self.retired) == 4:
                return
            if time.monotonic() >= deadline:
                self.error_class = self.error_class or "FinalUsageTimeout"
                self.stop.set()
                raise ValueError("Old final usage is not durably settled")
            await asyncio.sleep(self.limits["poll_seconds"])

    async def finish(self, *, success):
        self.done.set()
        if self.task is not None:
            await self.task
        try:
            await self.sample(tail=True)
            if success:
                if self.phase != "COMPLETED" or len(self.retired) != 4 or self.waiting:
                    raise ValueError("Complete 4+4 usage continuity evidence required")
                for row in self.members.values():
                    validate_usage(None, row["usage"], quiescent=True)
                self.ensure_running()
                self.verified = True
        except Exception as error:
            self.error_class = self.error_class or type(error).__name__
            self.stop.set()

    def report(self):
        counts = {}
        for uid, row in self.members.items():
            if "usage" not in row or uid not in self.baselines:
                continue
            for role, values in row["usage"]["counts"].items():
                base = self.baselines[uid]["counts"].get(role, [0] * len(FIELDS))
                total = counts.setdefault(role, [0] * len(FIELDS))
                for i, value in enumerate(values):
                    total[i] += value - base[i] if i != 3 else value
        return dict(
            verified=self.verified,
            error_class=self.error_class,
            samples=self.samples,
            evidence_bytes=self.bytes,
            counts=counts,
            counter_fields=list(FIELDS),
            retired_instances=len(self.retired),
            final_evidence_sha256=dict(self.saved),
            unresolved_instances=sorted(
                uid
                for uid, row in self.members.items()
                if uid not in self.retired
                and (row["slot"] == "blue" or row["state"] != "live")
            ),
            partial=not self.verified,
            scope="reported_sdk_usage_not_billing",
        )
