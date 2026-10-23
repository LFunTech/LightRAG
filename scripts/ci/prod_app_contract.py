"""Exact application behavior, allowing only explicit Kubernetes API defaults."""

from copy import deepcopy
import ipaddress


def drop_defaults(data, defaults):
    for key, value in defaults.items():
        if data.get(key) == value:
            data.pop(key, None)


def pod_spec(spec, *, bound=False):
    value = deepcopy(spec)
    drop_defaults(
        value,
        dict(
            restartPolicy="Always",
            dnsPolicy="ClusterFirst",
            schedulerName="default-scheduler",
            enableServiceLinks=True,
        ),
    )
    if value.get("serviceAccount") == value.get("serviceAccountName"):
        value.pop("serviceAccount", None)
    drop_defaults(value.get("securityContext", {}), {"fsGroupChangePolicy": "Always"})
    for container in value.get("containers", []) + value.get("initContainers", []):
        drop_defaults(
            container,
            dict(
                imagePullPolicy="IfNotPresent",
                terminationMessagePath="/dev/termination-log",
                terminationMessagePolicy="File",
            ),
        )
        for port in container.get("ports", []):
            drop_defaults(port, dict(protocol="TCP", hostPort=0, hostIP=""))
        for mount in container.get("volumeMounts", []):
            drop_defaults(mount, dict(readOnly=False, mountPropagation="None"))
        for name in ("startupProbe", "readinessProbe", "livenessProbe"):
            if name in container:
                probe = container[name]
                drop_defaults(
                    probe,
                    dict(
                        initialDelaySeconds=0,
                        timeoutSeconds=1,
                        periodSeconds=10,
                        successThreshold=1,
                        failureThreshold=3,
                    ),
                )
                if "httpGet" in probe:
                    drop_defaults(probe["httpGet"], dict(scheme="HTTP", host=""))
    for volume in value.get("volumes", []):
        for kind in ("secret", "configMap"):
            if kind in volume:
                drop_defaults(volume[kind], dict(defaultMode=420, optional=False))
    for constraint in value.get("topologySpreadConstraints", []):
        drop_defaults(
            constraint, dict(nodeAffinityPolicy="Honor", nodeTaintsPolicy="Ignore")
        )
    if bound:
        value.pop("nodeName", None)
        drop_defaults(value, dict(priority=0, preemptionPolicy="PreemptLowerPriority"))
        if "tolerations" in value:
            defaults = [
                dict(
                    effect="NoExecute",
                    key="node.kubernetes.io/" + key,
                    operator="Exists",
                    tolerationSeconds=300,
                )
                for key in ("not-ready", "unreachable")
            ]
            value["tolerations"] = [
                t for t in value["tolerations"] if t not in defaults
            ]
            if not value["tolerations"]:
                del value["tolerations"]
    return value


def template(value):
    result = deepcopy(value)
    drop_defaults(
        result.get("metadata", {}), {"creationTimestamp": None, "annotations": {}}
    )
    result["spec"] = pod_spec(result["spec"])
    return result


def application_matches(actual, expected):
    if actual.get("kind", expected["kind"]) != expected["kind"]:
        return False
    kind = expected["kind"]
    left, right = deepcopy(actual.get("spec", {})), deepcopy(expected["spec"])
    if kind == "Deployment":
        for value in (left, right):
            drop_defaults(
                value, dict(progressDeadlineSeconds=600, revisionHistoryLimit=10)
            )
            value["template"] = template(value["template"])
    if kind == "Service":
        for value in (left, right):
            drop_defaults(
                value,
                dict(
                    type="ClusterIP",
                    sessionAffinity="None",
                    ipFamilies=["IPv4"],
                    ipFamilyPolicy="SingleStack",
                    internalTrafficPolicy="Cluster",
                ),
            )
            for port in value.get("ports", []):
                drop_defaults(port, {"protocol": "TCP"})
            cluster_ip = value.get("clusterIP")
            try:
                ipaddress.ip_address(cluster_ip)
            except (TypeError, ValueError):
                continue
            if value.get("clusterIPs", [cluster_ip]) == [cluster_ip]:
                value.pop("clusterIP", None)
                value.pop("clusterIPs", None)
    annotations = actual.get("metadata", {}).get("annotations", {})
    return left == right and all(
        annotations.get(k) == v
        for k, v in expected.get("metadata", {}).get("annotations", {}).items()
    )
