"""Offline, explicit-input renderer. Does not connect to Kubernetes or import keys."""

import argparse
import json
from pathlib import Path

from .config import load_prod_config
from .render import render_entrypoint, render_foundation, render_slot, write_overlay


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--slot", choices=["blue", "green"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-onboarding", action="store_true")
    args = parser.parse_args(argv)
    config = load_prod_config(args.config)
    identity = json.loads(args.identity.read_text())
    env = json.loads(args.environment.read_text())
    resources = render_slot(
        config, identity, args.slot, image=args.image, environment=env
    )
    if args.include_onboarding:
        resources = (
            render_foundation(config) + render_entrypoint(config, args.slot) + resources
        )
    write_overlay(args.output, resources)
    print("Private overlay rendered; no resources applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
