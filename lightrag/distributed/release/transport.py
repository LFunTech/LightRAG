"""Production target adapter. Route proof comes from Kubernetes and CLB reads."""

import asyncio
import ipaddress
import json
import time
import urllib.request

from lightrag_delivery.kubernetes import selector_patch
from lightrag_delivery.qcloud import NoRedirect
from lightrag_delivery.render import labels, render_slot
from .models import ReleaseConflict, ReleaseUnavailable
from .store import json_value


class ProductionTransport:
    def __init__(self, *, config, kubernetes, cloud, store, image, environment):
        self.config, self.kube, self.cloud, self.store = (
            config,
            kubernetes,
            cloud,
            store,
        )
        self.image, self.environment = image, environment
        self.deadline = None

    async def call(self, function, *args, **kwargs):
        return await asyncio.to_thread(function, *args, **kwargs)

    async def preflight(self, identity):
        c = self.config
        if (
            await self.call(self.kube.namespace_uid, c.target["namespace"])
            != c.onboarding["namespace_uid"]
            or await self.call(self.kube.namespace_uid, "kube-system")
            != c.onboarding["kube_system_uid"]
        ):
            raise ReleaseConflict("Cluster or namespace UID differs")
        async with self.store.connection() as db:
            row = await self.store._scope(db, identity.scope)
            baseline = await db.fetchval(
                "SELECT manifest_hash FROM lightrag_coordination.workspaces WHERE deployment_id=$1 AND workspace=$2",
                *identity.scope.split("/"),
            )
            if (
                baseline != c.onboarding["baseline_manifest_hash"]
                or row["profile_hash"] != c.onboarding["profile_hash"]
            ):
                raise ReleaseConflict("Enrolled baseline or physical profile differs")
        expected_policy = dict(c.budget, writers=c.writers)
        if row["policy"] != expected_policy:
            raise ReleaseConflict(
                "Enrolled queue/capacity policy differs; separate onboarding approval is required"
            )
        ingress = await self.call(self.kube.get, "Ingress", "lightrag")
        if ingress is None:
            raise ReleaseConflict(
                "Explicit production entrypoint enrollment is required"
            )
        if ingress["spec"].get("tls"):
            raise ReleaseConflict("Application Kubernetes TLS is forbidden")
        annotations = ingress["metadata"].get("annotations", {})
        if (
            ingress["spec"].get("ingressClassName") != "qcloud"
            or annotations.get("ingress.cloud.tencent.com/direct-access") != "true"
            or annotations.get("kubernetes.io/ingress.qcloud-loadbalance-id")
            != c.ingress["load_balancer_id"]
        ):
            raise ReleaseConflict(
                "Ingress does not match the enrolled direct CLB route"
            )
        listeners = await self.call(
            self.cloud.call,
            "DescribeListeners",
            {
                "LoadBalancerId": c.ingress["load_balancer_id"],
                "ListenerIds": [c.ingress["listener_id"]],
            },
        )
        selected = [
            listener
            for listener in listeners.get("Listeners", [])
            if listener.get("ListenerId") == c.ingress["listener_id"]
        ]
        if (
            len(selected) != 1
            or selected[0].get("Protocol") != c.ingress["protocol"]
            or selected[0].get("Port")
            != (80 if c.ingress["mode"] == "edgeone" else 443)
        ):
            raise ReleaseConflict("Enrolled cloud origin listener is missing")
        rules = [
            r
            for r in selected[0].get("Rules", [])
            if r.get("LocationId") == c.ingress["location_id"]
        ]
        if (
            len(rules) != 1
            or rules[0].get("Domain") != c.target["host"]
            or rules[0].get("Url") != "/"
            or rules[0].get("ForwardType") != "HTTP"
            or rules[0].get("TargetType") != "NODE"
            or rules[0].get("SessionExpireTime") != 0
            or rules[0].get("RewriteTarget")
        ):
            raise ReleaseConflict(
                "CLB rule is not the admitted direct HTTP backend profile"
            )
        if c.ingress["mode"] == "edgeone":
            actual_ips = {
                item.get("ip")
                for item in ingress.get("status", {})
                .get("loadBalancer", {})
                .get("ingress", [])
            }
            if actual_ips != {c.ingress["edgeone"]["origin_address"]}:
                raise ReleaseConflict(
                    "EdgeOne origin differs from the enrolled Ingress VIP"
                )
            await self.call(self.cloud.verify_edgeone, c.ingress)
        # Controller and streaming/timeout evidence are explicitly enrolled,
        # not guessed from the test cluster or from Pod Ready.
        return row

    async def pods(self, slot):
        pods = await self.call(self.kube.list, "Pod", labels(self.config, slot))
        return [p for p in pods if not p["metadata"].get("deletionTimestamp")]

    async def local_status(self, pod):
        ip = pod.get("status", {}).get("podIP")
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            raise ReleaseUnavailable("Pod IP is missing") from None
        if ":" in ip:
            ip = "[" + ip + "]"

        def fetch():
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), NoRedirect()
            )
            request = urllib.request.Request(
                "http://" + ip + ":9621/release/status",
                headers={"X-API-Key": self.environment["LIGHTRAG_API_KEY"]},
            )
            try:
                with opener.open(request, timeout=3) as response:
                    body = response.read(65537)
                    if len(body) > 65536:
                        raise ValueError()
                    return json.loads(body)
            except Exception:
                raise ReleaseUnavailable(
                    "Direct Pod release status is unavailable"
                ) from None

        return await self.call(fetch)

    async def verified_pods(self, slot, *, config_hash=None, digest=None):
        pods = await self.pods(slot)
        if len(pods) != self.config.replicas:
            raise ReleaseUnavailable("A full slot is not available")
        result = []
        for pod in pods:
            containers = pod["spec"].get("containers", [])
            if (
                len(containers) != 1
                or not containers[0]["image"].startswith(self.image + "@sha256:")
                or (digest and containers[0]["image"] != self.image + "@" + digest)
            ):
                raise ReleaseConflict("Pod image is not the pinned candidate")
            runtime_status = pod.get("status", {}).get("containerStatuses", [])
            if len(runtime_status) != 1 or runtime_status[0].get("ready") is not True:
                raise ReleaseUnavailable("Container runtime readiness is not confirmed")
            expected_digest = containers[0]["image"].rsplit("@", 1)[1]
            actual_image = runtime_status[0].get("imageID", "")
            if not (
                actual_image.endswith("@" + expected_digest)
                or actual_image == "containerd://" + expected_digest
            ):
                raise ReleaseConflict(
                    "Running container image does not match its pinned specification"
                )
            status = await self.local_status(pod)
            if (
                status["instance_id"] != pod["metadata"]["uid"]
                or status["slot"] != slot
                or status["protocol_version"] != 1
                or status["profile_hash"] != self.config.onboarding["profile_hash"]
                or not status.get("shutdown_ready")
                or not status.get("readiness", {}).get("ready")
                or (config_hash and status["config_hash"] != config_hash)
            ):
                raise ReleaseUnavailable("Pod protocol, readiness or profile differs")
            result.append((pod, status))
        compatibility = {
            (s["command_formats_hash"], s["authorization_version"]) for _, s in result
        }
        if len(compatibility) != 1:
            raise ReleaseConflict("Slot command or authentication versions differ")
        return result

    async def prepare(self, identity, slot):
        row = await self.preflight(identity)
        if row["active_slot"] == slot or row["phase"] != "PREPARED":
            raise ReleaseConflict("Only the inactive slot may be prepared")
        baseline = await self.verified_pods(
            row["active_slot"], config_hash=row["config_hash"]
        )
        digests = {
            p["spec"]["containers"][0]["image"].rsplit("@", 1)[1] for p, _ in baseline
        }
        if len(digests) != 1:
            raise ReleaseConflict("Baseline artifact is not uniform")
        await self.store.capture_baseline(
            identity, expected_epoch=row["epoch"], digest=next(iter(digests))
        )
        rendered = render_slot(
            self.config,
            identity.as_dict(),
            slot,
            image=self.image,
            environment=self.environment,
        )
        for resource in rendered[:-1]:
            await self.call(self.kube.put_immutable, resource)
        deployment = rendered[-1]
        existing = await self.call(
            self.kube.get, "Deployment", deployment["metadata"]["name"]
        )
        if existing is None:
            await self.call(
                self.kube.request, "POST", self.kube.path("Deployment"), deployment
            )
        else:
            old = existing["spec"]["template"]["spec"]["containers"][0]
            desired = deployment["spec"]["template"]["spec"]["containers"][0]

            def managed_equal(a, b):
                if isinstance(b, dict):
                    return isinstance(a, dict) and all(
                        key in a and managed_equal(a[key], value)
                        for key, value in b.items()
                    )
                if isinstance(b, list):
                    return (
                        isinstance(a, list)
                        and len(a) == len(b)
                        and all(managed_equal(x, y) for x, y in zip(a, b))
                    )
                return a == b

            same = managed_equal(old, desired)
            if existing["spec"].get("replicas", 1) != 0 and not same:
                raise ReleaseConflict(
                    "Inactive slot still has a different live deployment"
                )
            if (
                old != desired
                or existing["spec"].get("replicas") != self.config.replicas
            ):
                await self.call(
                    self.kube.patch,
                    "Deployment",
                    deployment["metadata"]["name"],
                    [
                        {
                            "op": "test",
                            "path": "/metadata/resourceVersion",
                            "value": existing["metadata"]["resourceVersion"],
                        },
                        {"op": "replace", "path": "/spec", "value": deployment["spec"]},
                    ],
                )
        # Preparation is allowed to wait for scheduling, never to shrink Blue.
        end = time.monotonic() + self.config.budget["execution_seconds"]
        while True:
            try:
                await self.verify_candidate(identity, slot)
                return
            except ReleaseUnavailable:
                if time.monotonic() >= end:
                    raise
                await asyncio.sleep(1)

    async def verify_candidate(self, identity, slot):
        current = await self.store.snapshot(identity.scope)
        candidates = await self.verified_pods(
            slot, config_hash=identity.config_hash, digest=identity.digest
        )
        if current["active_slot"] != slot:
            old = await self.verified_pods(
                current["active_slot"],
                **await self.expected_slot(identity, current["active_slot"]),
            )
            if {
                (s["command_formats_hash"], s["authorization_version"]) for _, s in old
            } != {
                (s["command_formats_hash"], s["authorization_version"])
                for _, s in candidates
            }:
                raise ReleaseConflict(
                    "Cross-slot command/auth compatibility is unproven"
                )
        return [p["metadata"]["uid"] for p, _ in candidates]

    async def expected_slot(self, identity, slot):
        async with self.store.connection() as db:
            raw = await db.fetchval(
                "SELECT previous FROM lightrag_release.releases WHERE scope=$1 AND release_id=$2",
                identity.scope,
                identity.release_id,
            )
        if raw is None:
            raise ReleaseConflict("Durable release baseline is missing")
        previous = json_value(raw)
        if slot != previous["active_slot"]:
            return {"config_hash": identity.config_hash, "digest": identity.digest}
        if not previous.get("digest"):
            raise ReleaseConflict("Old artifact was not captured before preparation")
        return {"config_hash": previous["config_hash"], "digest": previous["digest"]}

    async def _observe(self, identity, slots):
        pods = []
        for slot in slots:
            pods.extend(
                p
                for p, _ in await self.verified_pods(
                    slot, **await self.expected_slot(identity, slot)
                )
            )
        expected = {p["metadata"]["uid"]: p for p in pods}
        slices = await self.call(
            self.kube.list, "EndpointSlice", {"kubernetes.io/service-name": "lightrag"}
        )
        endpoints = {}
        for item in slices:
            for point in item.get("endpoints", []):
                uid = point.get("targetRef", {}).get("uid")
                if (
                    point.get("conditions", {}).get("ready") is not True
                    or uid not in expected
                ):
                    raise ReleaseUnavailable("Endpoint propagation is incomplete")
                endpoints[uid] = set(point.get("addresses", []))
        if set(endpoints) != set(expected) or any(
            endpoints[uid] != {pod["status"]["podIP"]} for uid, pod in expected.items()
        ):
            raise ReleaseUnavailable("Endpoint Pod identity differs")
        try:
            proof = await self.call(
                self.cloud.observe,
                self.config.ingress,
                expected_ips={p["status"]["podIP"] for p in pods},
                port=9621,
            )
        except ValueError:
            raise ReleaseUnavailable("CLB propagation is not confirmed") from None
        return proof

    async def _wait_observe(self, identity, slots):
        end = time.monotonic() + self.config.budget["cutover_seconds"]
        while True:
            try:
                return await self._observe(identity, slots)
            except ReleaseUnavailable:
                if time.monotonic() >= end:
                    raise
                await asyncio.sleep(0.5)

    async def switch_read_route(self, identity, target):
        svc = await self.call(self.kube.get, "Service", "lightrag")
        if svc is None:
            raise ReleaseConflict("Stable Service is not enrolled")
        common = labels(self.config)
        old = svc["spec"]["selector"]
        if old.get("lightrag.io/slot") == target:
            return await self._wait_observe(identity, [target])
        if old != common:
            await self.call(
                self.kube.patch,
                "Service",
                "lightrag",
                selector_patch(svc["metadata"]["resourceVersion"], old, common),
            )
        # Make-before-break: all old backends remain until the full Green set
        # is both registered and healthy in CLB, not merely in EndpointSlice.
        await self._wait_observe(identity, ["blue", "green"])
        svc = await self.call(self.kube.get, "Service", "lightrag")
        await self.call(
            self.kube.patch,
            "Service",
            "lightrag",
            selector_patch(
                svc["metadata"]["resourceVersion"], common, labels(self.config, target)
            ),
        )
        return await self._wait_observe(identity, [target])

    async def verify_route(self, identity, target):
        svc = await self.call(self.kube.get, "Service", "lightrag")
        if svc is None or svc["spec"]["selector"] != labels(self.config, target):
            raise ReleaseConflict("Read route selector changed")
        proof = await self._wait_observe(identity, [target])
        from lightrag_delivery.qcloud import verify_entry_response

        pods = await self.pods(target)

        def entry():
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), NoRedirect()
            )
            request = urllib.request.Request(
                "https://" + self.config.target["host"] + "/release/status",
                headers={
                    "X-API-Key": self.environment["LIGHTRAG_API_KEY"],
                    "Connection": "close",
                },
            )
            try:
                with opener.open(request, timeout=3) as response:
                    raw = response.read(65537)
                    if len(raw) > 65536:
                        raise ValueError()
                    return json.loads(raw)
            except Exception:
                raise ReleaseUnavailable(
                    "Public HTTPS entry observation failed"
                ) from None

        response = await self.call(entry)
        try:
            verify_entry_response(
                response,
                slot=target,
                instance_ids={p["metadata"]["uid"] for p in pods},
                profile_hash=self.config.onboarding["profile_hash"],
                config_hash=(await self.expected_slot(identity, target))["config_hash"],
            )
        except ValueError:
            raise ReleaseUnavailable(
                "Public entry has not switched to the candidate set"
            ) from None
        return proof | {"entry_instance": response["instance_id"], "entry_slot": target}

    async def wait_closed_and_scale(self, identity, old_slot):
        end = time.monotonic() + self.config.budget["execution_seconds"]
        while True:
            async with self.store.connection() as db:
                unfinished = await db.fetchval(
                    "SELECT count(*) FROM lightrag_release.instances WHERE scope=$1 AND slot=$2 AND NOT closed",
                    identity.scope,
                    old_slot,
                )
            if not unfinished:
                break
            if time.monotonic() >= end:
                raise ReleaseUnavailable(
                    "Old instances have not closed; no scale or kill is allowed"
                )
            await asyncio.sleep(1)
        deployment = await self.call(
            self.kube.get, "Deployment", "lightrag-" + old_slot
        )
        if deployment is None:
            raise ReleaseConflict("Old deployment unexpectedly disappeared")
        if deployment["spec"]["replicas"] != 0:
            await self.call(
                self.kube.patch,
                "Deployment",
                "lightrag-" + old_slot,
                [
                    {
                        "op": "test",
                        "path": "/metadata/resourceVersion",
                        "value": deployment["metadata"]["resourceVersion"],
                    },
                    {"op": "replace", "path": "/spec/replicas", "value": 0},
                ],
            )

    async def verify_rollback_candidate(self, identity, target, approval):
        pods = await self.verified_pods(
            target, **await self.expected_slot(identity, target)
        )
        current = await self.store.snapshot(identity.scope)
        active = await self.verified_pods(
            current["active_slot"],
            config_hash=identity.config_hash,
            digest=identity.digest,
        )
        if {
            (s["command_formats_hash"], s["authorization_version"]) for _, s in pods
        } != {
            (s["command_formats_hash"], s["authorization_version"]) for _, s in active
        }:
            raise ReleaseConflict("Reverse command/auth compatibility differs")
        return [p["metadata"]["uid"] for p, _ in pods]
