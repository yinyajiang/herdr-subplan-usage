#!/usr/bin/env python3
"""Herdr Subscription Usage: Python 3.10+, no third-party dependencies."""
import argparse
import json
import os
from pathlib import Path
import sys

from subplan.display import text_view
from subplan.model import UsageError
from subplan.service import refresh
from subplan.storage import atomic_json, config_path


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("refresh", "json", "view", "dock-view", "ensure", "show", "init-config", "start", "stop", "watch"))
    args = parser.parse_args()
    try:
        if args.action == "init-config":
            path = config_path()
            if path.exists():
                print(f"已有配置，未覆盖：{path}")
            else:
                example = Path(__file__).parent / "config.example.json"
                atomic_json(path, json.loads(example.read_text(encoding="utf-8")))
                print(f"配置已创建：{path}")
        elif args.action in ("refresh", "json"):
            snapshot = refresh()
            if args.action == "json":
                print(json.dumps(snapshot, ensure_ascii=False))
            else:
                print(text_view(snapshot))
        elif args.action in ("view", "dock-view"):
            from subplan.viewer import view
            view(docked=args.action == "dock-view")
        elif args.action == "show":
            from subplan.herdr import start
            from subplan.dock import ensure, pane_preferences, set_collapsed
            start()
            pane = ensure(reveal=True)
            if pane and pane_preferences(pane).get("collapsed"):
                set_collapsed(pane, False)
        elif args.action == "ensure":
            from subplan.dock import ensure
            ensure()
        else:
            from subplan import herdr
            getattr(herdr, args.action)()
    except UsageError as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
