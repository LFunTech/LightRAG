"""Test-only model fixture and transparent HugeGraph timing proxy.

Deploy only in an isolated acceptance namespace. Business writes are forwarded
unchanged to real HugeGraph. Timing excludes the test barrier and response hold.
No production application code imports this module.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request

CONDITION = threading.Condition()
STATE = {
    "case": "bootstrap",
    "hold_llm": False,
    "graph_barrier": False,
    "fail_llm": False,
    "events": [],
    "graph_arrivals": [],
}
BACKEND = os.environ.get("HUGEGRAPH_BACKEND", "http://hugegraph:8080")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, payload, *, raw=False, headers=None):
        body = payload if raw else json.dumps(payload).encode()
        self.send_response(status)
        headers = headers or {}
        self.send_header(
            "Content-Type", headers.get("Content-Type", "application/json")
        )
        if headers.get("Content-Encoding"):
            self.send_header("Content-Encoding", headers["Content-Encoding"])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path == "/test/state":
            with CONDITION:
                self.reply(200, STATE)
        else:
            self.proxy()

    def do_DELETE(self):
        self.proxy()

    def do_PUT(self):
        self.proxy()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/test/control":
            patch = json.loads(body)
            allowed = {"case", "hold_llm", "graph_barrier", "fail_llm"}
            if set(patch) - allowed:
                self.reply(400, {"error": "unknown control"})
                return
            with CONDITION:
                if "case" in patch:
                    STATE["graph_arrivals"] = []
                STATE.update(patch)
                CONDITION.notify_all()
            self.reply(200, {"ok": True})
        elif self.path == "/v1/embeddings":
            data = json.loads(body)
            texts = data["input"]
            if isinstance(texts, str):
                texts = [texts]
            dimensions = int(data.get("dimensions", 8))
            self.reply(
                200,
                {
                    "object": "list",
                    "model": data.get("model", "k8s-fixture"),
                    "data": [
                        {
                            "object": "embedding",
                            "index": i,
                            "embedding": [1.0] * dimensions,
                        }
                        for i in range(len(texts))
                    ],
                    "usage": {"prompt_tokens": len(texts), "total_tokens": len(texts)},
                },
            )
        elif self.path == "/v1/chat/completions":
            self.completion(json.loads(body))
        else:
            self.proxy(body)

    def completion(self, data):
        messages = data["messages"]
        system = "\n".join(str(m["content"]) for m in messages if m["role"] == "system")
        prompt = str(messages[-1]["content"])
        extraction = "<|#|>" in system
        if extraction:
            event = {
                "kind": "extraction",
                "client": self.client_address[0],
                "case": STATE["case"],
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "start_ns": time.time_ns(),
            }
            with CONDITION:
                STATE["events"].append(event)
                if not CONDITION.wait_for(lambda: not STATE["hold_llm"], timeout=100):
                    self.reply(504, {"error": {"message": "test barrier deadline"}})
                    return
                fail = STATE["fail_llm"]
            if fail:
                # A normal provider failure, not an uncertain business write.
                event["end_ns"] = time.time_ns()
                self.reply(
                    400, {"error": {"message": "deliberate test extraction failure"}}
                )
                return
            suffix = ""
            if "K8S_INDEPENDENT_A" in prompt:
                suffix = "A"
            elif "K8S_INDEPENDENT_B" in prompt:
                suffix = "B"
            first, second = "Atlas" + suffix, "Borealis" + suffix
            content = (
                f"entity<|#|>{first}<|#|>organization<|#|>{first} research company.\n"
                f"entity<|#|>{second}<|#|>organization<|#|>{second} research company.\n"
                f"relation<|#|>{first}<|#|>{second}<|#|>cooperates<|#|>{first} cooperates with {second}.\n"
                "<|COMPLETE|>"
            )
            event["end_ns"] = time.time_ns()
        elif "high_level_keywords" in prompt:
            content = json.dumps(
                {
                    "high_level_keywords": ["cooperates"],
                    "low_level_keywords": ["Atlas", "Borealis"],
                }
            )
        else:
            content = "Atlas cooperates with Borealis on research."
        self.reply(
            200,
            {
                "id": "chatcmpl-k8s-fixture",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": data.get("model", "k8s-fixture"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    def proxy(self, body=None):
        if body is None and self.command in {"POST", "PUT", "DELETE"}:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))) or None
        client = self.client_address[0]
        if self.command == "POST" and self.path.endswith("/graph/vertices/batch"):
            with CONDITION:
                if STATE["graph_barrier"] and client not in STATE["graph_arrivals"]:
                    STATE["graph_arrivals"].append(client)
                    CONDITION.notify_all()
                    if not CONDITION.wait_for(
                        lambda: len(STATE["graph_arrivals"]) >= 2, timeout=60
                    ):
                        self.reply(
                            504,
                            {"error": "two independent graph writers never arrived"},
                        )
                        return
        headers = {
            name: self.headers[name]
            for name in ("Content-Type", "Authorization", "Accept")
            if name in self.headers
        }
        request = urllib.request.Request(
            BACKEND + self.path, data=body, headers=headers, method=self.command
        )
        event = {
            "kind": "graph",
            "method": self.command,
            "path": self.path,
            "client": client,
            "case": STATE["case"],
            "start_ns": time.time_ns(),
        }
        response_headers = {}
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                payload, status = response.read(), response.status
                response_headers = response.headers
        except urllib.error.HTTPError as error:
            payload, status = error.read(), error.code
            response_headers = error.headers
        except Exception as error:
            payload, status = json.dumps({"error": type(error).__name__}).encode(), 502
        event.update(end_ns=time.time_ns(), status=status)
        with CONDITION:
            STATE["events"].append(event)
        self.reply(status, payload, raw=True, headers=response_headers)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8090), Handler).serve_forever()
