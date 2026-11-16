"""Command-line interface for FormSpec generator."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

from formspec.db import get_adapter
from formspec.generators import TargetType, generate_for_entity
from formspec.ide.server import IDEApp, create_server, resolve_default_dsn
from formspec.runtime.server import AppRuntime, create_app_server

_SYSTEM_TABLES = {
    "entity",
    "forms",
    "server_module",
    "client_module",
    "server_action",
    "client_action",
    "users",
    "roles",
    "permissions",
    "user_roles",
    "role_permissions",
    "user_role",
    "role_permission",
    "role",
    "permission",
}


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="formspec",
        description="Generate FormSpec (.fspec) forms and server action scripts from a database.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    gen = subparsers.add_parser(
        "generate",
        help="Generate .fspec and server action script(s) for one or more entities.",
    )
    gen.add_argument(
        "--db",
        required=True,
        help="Database DSN or file path (e.g. sqlite:///app.db).",
    )
    gen.add_argument(
        "--entity",
        action="append",
        default=[],
        help="Target singular entity/table name (repeatable).",
    )
    gen.add_argument(
        "--all",
        action="store_true",
        help="Generate forms for all non-RBAC domain entity tables.",
    )
    gen.add_argument(
        "--out-dir",
        default="./generated",
        help="Directory to write generated .fspec and action files (default: ./generated).",
    )
    gen.add_argument(
        "--target",
        choices=("python", "node", "both"),
        default="python",
        help="Server action runtime target (default: python).",
    )
    gen.add_argument(
        "--include-pk",
        action="store_true",
        help="Include primary key column in the generated .fspec form.",
    )
    gen.add_argument(
        "--layout",
        choices=("single", "two-col"),
        default="single",
        help="Field layout style in generated .fspec (default: single).",
    )

    ide_cmd = subparsers.add_parser(
        "ide",
        help="Launch the FormSpec Prototype IDE web application.",
    )
    ide_cmd.add_argument(
        "--db",
        default=None,
        help="Database connection string (defaults to FORMSPEC_DATABASE_URL, DATABASE_URL, or sqlite:///formspec_studio.db).",
    )
    ide_cmd.add_argument(
        "--host",
        default=os.environ.get("FORMSPEC_HOST") or os.environ.get("HOST") or "127.0.0.1",
        help="Bind host address (default: 127.0.0.1).",
    )
    ide_cmd.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("FORMSPEC_PORT") or os.environ.get("PORT") or "8765"),
        help="HTTP port (default: 8765 or PORT env var).",
    )
    ide_cmd.add_argument(
        "--formatters-dir",
        default=None,
        help="Directory containing custom Python FormFormatter plugins.",
    )
    ide_cmd.add_argument(
        "--styles-dir",
        default=None,
        help="Directory containing custom .css theme stylesheets.",
    )

    app_cmd = subparsers.add_parser(
        "app",
        help="Run the packaged standalone FormSpec web application (not the IDE).",
    )
    app_cmd.add_argument(
        "--db",
        default=None,
        help="Database connection string (defaults to DATABASE_CONNECTION_STRING or sqlite:///./data/app.db).",
    )
    app_cmd.add_argument(
        "--host",
        default=os.environ.get("FORMSPEC_HOST") or os.environ.get("HOST") or "0.0.0.0",
        help="Bind host address (default: 0.0.0.0).",
    )
    app_cmd.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("FORMSPEC_PORT") or os.environ.get("PORT") or "8080"),
        help="HTTP port (default: 8080 or PORT env var).",
    )
    app_cmd.add_argument(
        "--init-sql",
        default=None,
        help="Optional explicit path to DB initialization SQL script.",
    )
    app_cmd.add_argument(
        "--auth-provider",
        default=None,
        help="Authentication provider ID ('local' or 'easyauth').",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "app":
        runtime = AppRuntime(
            dsn=args.db,
            init_sql_path=args.init_sql,
            auth_provider=args.auth_provider,
        )
        server = create_app_server(runtime, host=args.host, port=args.port)
        print(
            f"FormSpec App running at http://{args.host}:{args.port} "
            f"(db: {runtime.dsn}, initialized_on_launch={runtime.db_initialized_on_startup})"
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0

    if args.command == "ide":
        dsn = resolve_default_dsn(args.db)
        app = IDEApp(
            dsn=dsn,
            formatters_dir=args.formatters_dir,
            styles_dir=args.styles_dir,
        )
        server = create_server(app, host=args.host, port=args.port)
        print(f"FormSpec Studio running at http://{args.host}:{args.port} (db: {dsn})")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0

    if args.command == "generate":
        entities: list[str] = list(args.entity)
        if args.all:
            with get_adapter(args.db) as adapter:
                entities = [
                    t for t in adapter.list_tables() if t not in _SYSTEM_TABLES
                ]
        if not entities:
            parser.error("Provide at least one --entity <name> or --all.")

        target: TargetType = args.target
        out_dir = Path(args.out_dir)
        for entity_slug in entities:
            result = generate_for_entity(
                dsn=args.db,
                entity_slug=entity_slug,
                out_dir=out_dir,
                target=target,
                include_pk=args.include_pk,
                layout=args.layout,
            )
            generated_list = ", ".join(
                [str(result.fspec_path), *(str(p) for p in result.action_paths)]
            )
            print(f"Generated [{result.entity_slug}]: {generated_list}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
