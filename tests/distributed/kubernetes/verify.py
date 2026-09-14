"""Run real multi-node Pod acceptance against an already bootstrapped test chart.

Requires the test-only gateway and isolated infrastructure from README.md.
Never point this runner at a production workspace: it creates documents and
replaces one application Pod. Credentials are read from a local protected file.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid


def request(url, *, method="GET", payload=None, key=None, timeout=100):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-API-Key"] = key
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def primary_documents(documents):
    """Validate duplicate audit rows without treating them as failed primary docs."""
    primary = [d for d in documents if not d.get("metadata", {}).get("is_duplicate")]
    ids = {d["id"] for d in primary}
    for document in documents:
        metadata = document.get("metadata", {})
        if metadata.get("is_duplicate"):
            assert document["id"].startswith("dup-"), document
            assert document["status"] == "failed", document
            assert document["chunks_count"] == 0, document
            assert metadata["original_doc_id"] in ids, document
    return primary


def eventually(probe, *, timeout=180):
    deadline = time.monotonic() + timeout
    result = None
    while time.monotonic() < deadline:
        result = probe()
        if result:
            return result
        time.sleep(0.2)
    raise AssertionError(f"condition did not converge: {result}")


class Cluster:
    def __init__(self, args):
        if args.context != "kind-lightrag-concurrency" or not args.namespace.startswith(
            "lightrag-concurrency-test"
        ):
            raise ValueError(
                "Only the explicitly isolated local acceptance cluster is allowed"
            )
        self.base = [
            "kubectl",
            "--kubeconfig",
            args.kubeconfig,
            "--context",
            args.context,
            "-n",
            args.namespace,
        ]

    def run(self, *args):
        return subprocess.check_output(self.base + list(args), text=True)

    def pods(self):
        data = json.loads(
            self.run(
                "get", "pods", "-l", "app.kubernetes.io/instance=lightrag", "-o", "json"
            )
        )
        return [
            p
            for p in data["items"]
            if p["status"].get("phase") == "Running"
            and not p["metadata"].get("deletionTimestamp")
            and any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in p["status"].get("conditions", [])
            )
        ]

    @contextmanager
    def forward(self, target, remote_port):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen(
            self.base
            + [
                "port-forward",
                "--address",
                "127.0.0.1",
                target,
                f"{port}:{remote_port}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:

            def connected():
                if process.poll() is not None:
                    raise RuntimeError("port-forward exited")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        return True
                except OSError:
                    return False

            eventually(connected, timeout=30)
            yield f"http://127.0.0.1:{port}"
        finally:
            process.terminate()
            process.wait(timeout=10)

    def python(self, pod, code):
        return self.run("exec", pod, "--", "python", "-c", code)


def audit(cluster, pod, ids):
    code = f"""
import asyncio,json
from lightrag.distributed.__main__ import configured_rag
from lightrag.constants import GRAPH_FIELD_SEP
from lightrag.utils import compute_mdhash_id
async def main():
    rag=configured_rag()
    await rag.initialize_storages()
    result={{"separator":GRAPH_FIELD_SEP,"node":await rag.chunk_entity_relation_graph.get_node("Atlas"),"edge":await rag.chunk_entity_relation_graph.get_edge("Atlas","Borealis"),"tracking":await rag.entity_chunks.get_by_id("Atlas"),"anchors":{{i:await rag.full_entities.get_by_id(i) for i in {ids!r}}},"relation_anchors":{{i:await rag.full_relations.get_by_id(i) for i in {ids!r}}},"vector":await rag.entities_vdb.get_by_id(compute_mdhash_id("Atlas",prefix="ent-"))}}
    await rag.finalize_storages()
    print("ACCEPTANCE_JSON="+json.dumps(result,default=str))
asyncio.run(main())
"""
    output = cluster.python(pod, code)
    return json.loads(
        next(
            line.split("=", 1)[1]
            for line in output.splitlines()
            if line.startswith("ACCEPTANCE_JSON=")
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", default="kind-lightrag-concurrency")
    parser.add_argument("--namespace", default="lightrag-concurrency-test")
    parser.add_argument("--credentials", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    credentials = dict(
        line.split("=", 1)
        for line in Path(args.credentials).read_text().splitlines()
        if "=" in line
    )
    key = credentials["LIGHTRAG_API_KEY"]
    cluster = Cluster(args)
    pods = eventually(lambda: rows if len(rows := cluster.pods()) == 2 else None)
    assert len({p["spec"]["nodeName"] for p in pods}) == 2, (
        "writers must run on different nodes"
    )
    identities = [
        {
            "name": p["metadata"]["name"],
            "uid": p["metadata"]["uid"],
            "node": p["spec"]["nodeName"],
            "ip": p["status"]["podIP"],
            "image_id": p["status"]["containerStatuses"][0]["imageID"],
        }
        for p in pods
    ]
    result = {"run_id": uuid.uuid4().hex, "pods": identities, "checks": []}

    def check(description):
        result["checks"].append(description)
        print("PASS: " + description, flush=True)

    first, second = [p["name"] for p in identities]
    expected = hashlib.sha256(
        Path("lightrag/distributed/control.py").read_bytes()
    ).hexdigest()
    for pod in (first, second):
        actual = cluster.python(
            pod,
            'import hashlib,lightrag.distributed.control as m; print(hashlib.sha256(open(m.__file__,"rb").read()).hexdigest())',
        ).strip()
        assert actual == expected
    result["control_source_sha256"] = expected
    probe = f"/app/data/inputs/acceptance-{uuid.uuid4().hex}.probe"
    cluster.python(
        first,
        f'from pathlib import Path; Path({probe!r}).write_text("shared-cross-node")',
    )
    assert (
        cluster.python(
            second, f"from pathlib import Path; print(Path({probe!r}).read_text())"
        ).strip()
        == "shared-cross-node"
    )
    exclusive = cluster.python(
        second,
        f'import os\ntry:\n os.open({probe!r},os.O_CREAT|os.O_EXCL|os.O_WRONLY)\nexcept FileExistsError:\n print("exclusive-create-refused")',
    )
    assert exclusive.strip() == "exclusive-create-refused"
    cluster.python(first, f"from pathlib import Path; Path({probe!r}).unlink()")
    check("different nodes, exact source, coherent shared filesystem")
    with ExitStack() as stack:
        urls = [
            stack.enter_context(cluster.forward("pod/" + p, 9621))
            for p in (first, second)
        ]
        gateway = stack.enter_context(cluster.forward("service/gateway", 8090))

        def api(i, path, method="GET", payload=None):
            return request(urls[i] + path, method=method, payload=payload, key=key)

        def control(**patch):
            if "case" in patch:
                patch["case"] = result["run_id"] + ":" + patch["case"]
            assert (
                request(gateway + "/test/control", method="POST", payload=patch)[0]
                == 200
            )

        def events(case):
            status, state = request(gateway + "/test/state")
            assert status == 200
            return [
                e for e in state["events"] if e["case"] == result["run_id"] + ":" + case
            ]

        def rows():
            status, data = api(
                0, "/documents/paginated", "POST", {"page": 1, "page_size": 50}
            )
            assert status == 200, data
            primary = primary_documents(data["documents"])
            result["duplicate_audits"] = [
                d
                for d in data["documents"]
                if d.get("metadata", {}).get("is_duplicate")
            ]
            return primary

        def processed(total, *, retrying=None):
            status, control_state = api(0, "/documents/pipeline_status")
            assert status == 200 and not control_state["distributed"]["fenced"], (
                control_state
            )
            docs = rows()
            failures = [
                d
                for d in docs
                if d["status"] == "failed"
                and not (
                    retrying is not None
                    and d["id"] == retrying["id"]
                    and d["updated_at"] == retrying["updated_at"]
                )
            ]
            assert not failures, failures
            return (
                docs
                if len(docs) == total and all(d["status"] == "processed" for d in docs)
                else None
            )

        def submit_pair(texts, sources):
            with ThreadPoolExecutor(2) as executor:
                futures = [
                    executor.submit(
                        api,
                        i,
                        "/documents/text",
                        "POST",
                        {"text": texts[i], "file_source": sources[i]},
                    )
                    for i in range(2)
                ]
                return [future.result() for future in futures]

        def wait_two(case):
            eventually(
                lambda: (
                    len(
                        {e["client"] for e in events(case) if e["kind"] == "extraction"}
                    )
                    == 2
                ),
                timeout=60,
            )

        try:
            assert not rows(), "use a fresh isolated workspace"
            control(case="independent", hold_llm=True, graph_barrier=True)
            responses = submit_pair(
                [
                    "K8S_INDEPENDENT_A AtlasA cooperates with BorealisA.",
                    "K8S_INDEPENDENT_B AtlasB cooperates with BorealisB.",
                ],
                ["independent-a.txt", "independent-b.txt"],
            )
            assert all(r[0] == 200 for r in responses), responses
            wait_two("independent")
            status, state = api(1, "/documents/pipeline_status")
            assert status == 200 and state["distributed"]["claimed_documents"] == 2, (
                state
            )
            start = time.monotonic()
            denied = api(0, "/documents", "DELETE")
            assert (
                denied[0] == 409
                and denied[1]["detail"]["error"] == "CoordinationBusyError"
            ), denied
            result["maintenance_refusal_seconds"] = round(time.monotonic() - start, 3)
            control(hold_llm=False)
            eventually(lambda: processed(2))
            writes = [
                e
                for e in events("independent")
                if e["kind"] == "graph"
                and e["method"] == "POST"
                and e["path"].endswith("/graph/vertices/batch")
                and 200 <= e["status"] < 300
            ]
            overlaps = [
                (
                    a,
                    b,
                    min(a["end_ns"], b["end_ns"]) - max(a["start_ns"], b["start_ns"]),
                )
                for a in writes
                for b in writes
                if a["client"] != b["client"]
                and max(a["start_ns"], b["start_ns"]) < min(a["end_ns"], b["end_ns"])
            ]
            assert overlaps, writes
            a, b, duration = max(overlaps, key=lambda row: row[2])
            result["physical_http_overlap"] = {
                "requests": [a, b],
                "overlap_ns": duration,
            }
            check(
                "two document claims, real graph HTTP overlap, clear rejected before admission"
            )

            control(case="shared", hold_llm=True, graph_barrier=False)
            responses = submit_pair(
                [
                    "Atlas cooperates with Borealis on shared experiment one.",
                    "Atlas cooperates with Borealis on shared experiment two.",
                ],
                ["shared-a.txt", "shared-b.txt"],
            )
            assert all(r[0] == 200 for r in responses), responses
            wait_two("shared")
            control(hold_llm=False)
            docs = eventually(lambda: processed(4))
            ids = [
                d["id"]
                for d in docs
                if d["file_path"] in {"shared-a.txt", "shared-b.txt"}
            ]
            assert len(ids) == 2, docs
            current = audit(cluster, first, ids)
            sources = set(current["edge"]["source_id"].split(current["separator"]))
            assert len(sources) == 2 and current["edge"]["weight"] >= 2, current
            assert set(current["tracking"]["chunk_ids"]) == sources
            assert (
                all(current["anchors"].values())
                and all(current["relation_anchors"].values())
                and current["vector"]
            )
            result["shared_evidence"] = current
            check(
                "shared entity/relation source union, weight, tracking, both anchors and real vector"
            )

            control(case="duplicate", hold_llm=False, graph_barrier=False)
            responses = submit_pair(
                ["K8S_DUPLICATE_RACE Atlas cooperates with Borealis."] * 2,
                ["duplicate-race.txt"] * 2,
            )
            assert any(r[0] == 200 for r in responses) and all(
                r[0] in {200, 409} for r in responses
            ), responses
            eventually(lambda: processed(5))
            assert (
                len([e for e in events("duplicate") if e["kind"] == "extraction"]) == 1
            ), events("duplicate")
            repeated = submit_pair(
                ["K8S_DUPLICATE_RACE Atlas cooperates with Borealis."] * 2,
                ["duplicate-race.txt"] * 2,
            )
            assert all(r[0] == 409 for r in repeated), repeated
            assert (
                len([e for e in events("duplicate") if e["kind"] == "extraction"]) == 1
            )
            result["duplicate_responses"] = [r[0] for r in responses]
            check(
                "competing identical submissions execute extraction once; replay does not add evidence"
            )

            control(case="pause", hold_llm=False, graph_barrier=False)
            assert api(0, "/documents/cancel_pipeline", "POST")[0] == 200
            assert api(1, "/documents/pipeline_status")[1]["distributed"]["paused"]
            assert (
                api(
                    0,
                    "/documents/text",
                    "POST",
                    {
                        "text": "Atlas cooperates with Borealis after explicit resume.",
                        "file_source": "paused.txt",
                    },
                )[0]
                == 200
            )
            time.sleep(2)
            paused = [d for d in rows() if d["file_path"] == "paused.txt"]
            assert len(paused) == 1 and paused[0]["status"] == "pending", paused
            assert not [e for e in events("pause") if e["kind"] == "extraction"]
            assert api(1, "/documents/reprocess_failed", "POST")[0] == 200
            eventually(lambda: processed(6))
            check("global pause survives peer polling/enqueue; explicit retry resumes")

            control(case="failed", fail_llm=True)
            assert (
                api(
                    1,
                    "/documents/text",
                    "POST",
                    {
                        "text": "Atlas cooperates with Borealis after one failed extraction.",
                        "file_source": "failed.txt",
                    },
                )[0]
                == 200
            )
            failed = eventually(
                lambda: next(
                    (
                        d
                        for d in rows()
                        if d["file_path"] == "failed.txt" and d["status"] == "failed"
                    ),
                    None,
                )
            )
            count = len([e for e in events("failed") if e["kind"] == "extraction"])
            time.sleep(2)
            assert (
                next(d for d in rows() if d["id"] == failed["id"])["updated_at"]
                == failed["updated_at"]
            )
            assert (
                len([e for e in events("failed") if e["kind"] == "extraction"]) == count
            )
            control(fail_llm=False)
            assert api(0, "/documents/reprocess_failed", "POST")[0] == 200
            # Acceptance of a durable retry is asynchronous, not completion.
            # Allow only the exact pre-retry FAILED version while it is picked up.
            docs = eventually(lambda: processed(7, retrying=failed))
            assert (
                len([e for e in events("failed") if e["kind"] == "extraction"])
                == count + 1
            )
            check("FAILED does not automatically retry; explicit request converges")
            result["documents"] = [
                {k: d[k] for k in ("id", "file_path", "status")} for d in docs
            ]
            result["final_control"] = api(1, "/documents/pipeline_status")[1][
                "distributed"
            ]
            assert not result["final_control"]["fenced"]
        finally:
            control(hold_llm=False, graph_barrier=False, fail_llm=False)
    cluster.run("delete", "pod", first, "--wait=true", "--timeout=120s")
    replacements = eventually(
        lambda: (
            rows
            if len(rows := cluster.pods()) == 2
            and all(p["metadata"]["uid"] != identities[0]["uid"] for p in rows)
            else None
        )
    )
    result["replacement_pods"] = [
        {
            "name": p["metadata"]["name"],
            "uid": p["metadata"]["uid"],
            "node": p["spec"]["nodeName"],
        }
        for p in replacements
    ]
    with cluster.forward("pod/" + replacements[0]["metadata"]["name"], 9621) as url:
        status, data = request(
            url + "/documents/paginated",
            method="POST",
            payload={"page": 1, "page_size": 50},
            key=key,
        )
        assert (
            status == 200
            and len(primary_documents(data["documents"])) == 7
            and all(
                d["status"] == "processed" for d in primary_documents(data["documents"])
            )
        ), data
    check("graceful Pod replacement retains all seven processed documents")
    Path(args.output).write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(
        json.dumps(
            {
                "passed_checks": len(result["checks"]),
                "output": args.output,
                "overlap_ns": result["physical_http_overlap"]["overlap_ns"],
            }
        )
    )


if __name__ == "__main__":
    main()
