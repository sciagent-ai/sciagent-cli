"""Handler for `sciagent config show` and `sciagent config keys`."""

from __future__ import annotations

import argparse
import json
import sys

from ..config import ConfigError, list_config_keys, load_config


def handle_config(args: argparse.Namespace) -> int:
    if args.config_verb == "show":
        return _show(args)
    if args.config_verb == "keys":
        return _keys(args)
    print("Usage: sciagent config {show|keys}", file=sys.stderr)
    return 1


def _show(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(
            explicit_path=args.config,
            project_dir=args.project_dir,
            overrides=args.overrides,
        )
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(cfg.to_dict(), indent=2, default=str))
    else:
        print(cfg.to_yaml(), end="")
    return 0


def _keys(args: argparse.Namespace) -> int:
    catalog = list_config_keys()
    if args.format == "json":
        print(json.dumps(catalog, indent=2, default=str))
        return 0

    for section, items in catalog.items():
        print(f"[{section}]")
        for entry in items:
            default_repr = entry["default"]
            if default_repr is None:
                default_str = "null"
            else:
                default_str = repr(default_repr)
            print(f"  {entry['name']}: {entry['type']} (default: {default_str})")
        print()
    return 0
