"""Command line: `serve` runs the proxy, `report` prints the measurement report."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from .config import load_settings


def load_dotenv(path: str | Path = ".env") -> None:
    """Load KEY=VALUE lines into the environment. Existing variables win."""
    env_file = Path(path)
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import create_app

    load_dotenv(args.env_file)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_app(load_settings(args.config))
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def _report(args: argparse.Namespace) -> int:
    from .report import build_report, render_text, since_iso
    from .store import RequestStore

    db_path = args.db or load_settings(args.config).db_path
    store = RequestStore(db_path)
    try:
        rep = build_report(store.fetch(since_iso(args.days)), days=args.days, tenant=args.tenant)
    finally:
        store.close()
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    print(json.dumps(rep, ensure_ascii=False, indent=2) if args.json else render_text(rep))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="llm-gateway", description="Enterprise LLM gateway, phase 1")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the proxy")
    serve.add_argument("--config", default=None, help="YAML config path (default: config.yaml or $GATEWAY_CONFIG)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--log-level", default="info")
    serve.add_argument("--env-file", default=".env", help="file with provider keys (default: .env)")
    serve.set_defaults(func=_serve)

    report = sub.add_parser("report", help="print the measurement report")
    report.add_argument("--config", default=None)
    report.add_argument("--db", default=None, help="SQLite path, overrides the config")
    report.add_argument("--days", type=float, default=7.0)
    report.add_argument("--tenant", default=None)
    report.add_argument("--json", action="store_true", help="machine-readable output")
    report.set_defaults(func=_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
