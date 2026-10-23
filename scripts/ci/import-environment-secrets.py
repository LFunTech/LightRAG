#!/usr/bin/env python3
"""Plan environment-specific CI secrets; remote writes require explicit --apply."""

import argparse
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.ci.prod_config import (  # noqa: E402
    ProdConfigError,
    parse_secret_inventory,
    secret_aliases,
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ProdConfigError("CI: redirects forbidden")


def apply_secrets(values, *, environment, server, repo_id, token, images, opener=None):
    prefix = f"lightrag_{environment}_"
    parsed = urlsplit(server)
    if (
        environment not in {"prod", "test"}
        or not values
        or any(
            not name.startswith(prefix) or not re.fullmatch(r"[a-z0-9_]+", name)
            for name in values
        )
    ):
        raise ProdConfigError("CI: cross-environment secret name rejected")
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or type(repo_id) is not int
        or repo_id < 1
        or not token
        or not images
    ):
        raise ProdConfigError(
            "CI: explicit HTTPS target, repository, token and image restrictions required"
        )
    opener = opener or urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect()
    )
    base = server.rstrip("/") + f"/api/repos/{repo_id}/secrets"

    def request(url, method, data=None):
        req = urllib.request.Request(
            url,
            method=method,
            data=json.dumps(data).encode() if data else None,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with opener.open(req, timeout=30) as response:
                return json.loads(response.read())
        except (OSError, ValueError):
            raise ProdConfigError(
                "CI: secret request failed; no automatic retry"
            ) from None

    existing = request(base, "GET")
    if not isinstance(existing, list):
        raise ProdConfigError("CI: invalid secret-list response")
    names = {x.get("name") for x in existing if isinstance(x, dict)}
    for name, value in sorted(values.items()):
        request(
            base + ("/" + name if name in names else ""),
            "PATCH" if name in names else "POST",
            {"name": name, "value": value, "events": ["tag"], "images": images},
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=["prod", "test"], required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--api-key-alias", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--server")
    parser.add_argument("--repo-id", type=int)
    parser.add_argument("--image", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        aliases = secret_aliases(args.environment, api_key_alias=args.api_key_alias)
        values = parse_secret_inventory(
            args.input,
            environment=args.environment,
            aliases=aliases,
            required={"api_key", "llm_api_key"},
        )
        if args.apply:
            apply_secrets(
                values,
                environment=args.environment,
                server=args.server or "",
                repo_id=args.repo_id,
                token=os.getenv("WOODPECKER_TOKEN"),
                images=args.image,
            )
        print(
            json.dumps(
                {
                    "environment": args.environment,
                    "mode": "applied" if args.apply else "plan",
                    "names": sorted(values),
                }
            )
        )
        return 0
    except ProdConfigError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
