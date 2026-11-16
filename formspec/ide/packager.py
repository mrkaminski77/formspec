"""Docker bundle and multi-RDBMS init-script packager for FormSpec web apps."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any
import uuid
import zipfile

from formspec.db.base import WorkspaceAdapter
from formspec.generators.db_init import generate_db_init_sql, get_dialect

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class PackagedAppBundle:
    """Metadata for a packaged standalone web application bundle."""

    bundle_id: str
    app_name: str
    image_tag: str
    dialect: str
    dialect_label: str
    bundle_dir: Path
    zip_path: Path
    init_sql_filename: str
    init_sql: str
    docker_built: bool
    docker_log: str


def _sanitize_slug(raw: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9._-]+", "-", (raw or "formspec-app").strip()).strip("-.").lower()
    return clean or "formspec-app"


def _copy_runtime_tree(dest_root: Path) -> None:
    """Copy minimal standalone runtime Python & asset files (excluding IDE SPA)."""
    src_pkg = _PACKAGE_ROOT
    dst_pkg = dest_root / "formspec"

    def _ignore(directory: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        rel = Path(directory).relative_to(src_pkg) if Path(directory) != src_pkg else Path(".")
        for name in names:
            if name == "__pycache__" or name.endswith((".pyc", ".pyo", ".db")):
                ignored.add(name)
            # Exclude IDE static UI from the standalone web app container, keeping themes CSS
            if rel == Path("ide") and name == "static":
                ignored.add(name)
        return ignored

    shutil.copytree(src_pkg, dst_pkg, ignore=_ignore, dirs_exist_ok=True)
    pyproject_src = _PROJECT_ROOT / "pyproject.toml"
    if pyproject_src.is_file():
        shutil.copy2(pyproject_src, dest_root / "pyproject.toml")


def _build_dockerfile(dialect_id: str) -> str:
    extra_pip = ""
    if dialect_id == "postgres":
        extra_pip = 'RUN pip install --no-cache-dir "psycopg[binary]>=3.1"\n'
    elif dialect_id == "mysql":
        extra_pip = 'RUN pip install --no-cache-dir "pymysql>=1.1"\n'
    elif dialect_id == "mssql":
        extra_pip = 'RUN pip install --no-cache-dir "pyodbc>=5.0"\n'

    return f"""# Packaged standalone FormSpec Web Application (not the IDE)
FROM python:3.12-slim

WORKDIR /app

# Install Node.js 22 runtime for stored Node.js server actions
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \\
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \\
    && apt-get install -y --no-install-recommends nodejs \\
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY formspec ./formspec
COPY app_data ./app_data

RUN pip install --no-cache-dir --no-build-isolation . && mkdir -p /app/data
{extra_pip}
# Defaults to SQLite (/app/data/app.db) unless DATABASE_CONNECTION_STRING is supplied
ENV HOST="0.0.0.0" \\
    PORT="8080" \\
    FORMSPEC_AUTH_PROVIDER="local"

VOLUME ["/app/data"]
EXPOSE 8080

CMD ["python3", "-m", "formspec", "app"]
"""


def _build_compose_yml(image_tag: str, dialect_id: str) -> str:
    return f"""services:
  app:
    build: .
    image: {image_tag}
    ports:
      - "8080:8080"
    environment:
      # Leave DATABASE_CONNECTION_STRING unset to use embedded SQLite (/app/data/app.db)
      DATABASE_CONNECTION_STRING: "${{DATABASE_CONNECTION_STRING:-}}"
      FORMSPEC_AUTH_PROVIDER: "${{FORMSPEC_AUTH_PROVIDER:-local}}"
      FORMSPEC_ADMIN_PASSWORD: "${{FORMSPEC_ADMIN_PASSWORD:-}}"
      FORMSPEC_SECRET: "${{FORMSPEC_SECRET:-}}"
    volumes:
      - app_data_vol:/app/data
    # Target RDBMS dialect packaged in ./app_data/init_db.{dialect_id}.sql

volumes:
  app_data_vol:
"""


class AppPackager:
    """Creates standalone Docker build bundles and tracks downloadable archives."""

    def __init__(self, staging_root: Path | None = None) -> None:
        self._owns_root = staging_root is None
        self._staging_root = staging_root or Path(tempfile.mkdtemp(prefix="formspec_pkg_"))
        self._staging_root.mkdir(parents=True, exist_ok=True)
        self._bundles: dict[str, PackagedAppBundle] = {}

    def cleanup(self) -> None:
        if self._owns_root and self._staging_root.exists():
            shutil.rmtree(self._staging_root, ignore_errors=True)

    def get_bundle(self, bundle_id: str) -> PackagedAppBundle | None:
        return self._bundles.get(bundle_id)

    def package(
        self,
        adapter: WorkspaceAdapter,
        *,
        app_name: str = "formspec-app",
        dialect_id: str = "sqlite",
        include_data: bool = False,
        build_docker: bool = False,
    ) -> PackagedAppBundle:
        dialect = get_dialect(dialect_id)
        slug = _sanitize_slug(app_name)
        image_tag = slug if ":" in slug else f"{slug}:latest"
        bundle_id = uuid.uuid4().hex[:12]

        bundle_dir = self._staging_root / f"{slug}_{bundle_id}"
        app_data_dir = bundle_dir / "app_data"
        app_data_dir.mkdir(parents=True, exist_ok=True)

        _copy_runtime_tree(bundle_dir)

        init_filename = f"init_db.{dialect.id}.sql"
        init_sql = generate_db_init_sql(
            adapter, dialect.id, include_entity_rows=include_data
        )
        (app_data_dir / init_filename).write_text(init_sql, encoding="utf-8")

        # Also bundle SQLite init fallback so container self-initializes if run without DATABASE_CONNECTION_STRING
        if dialect.id != "sqlite":
            sqlite_sql = generate_db_init_sql(
                adapter, "sqlite", include_entity_rows=include_data
            )
            (app_data_dir / "init_db.sqlite.sql").write_text(sqlite_sql, encoding="utf-8")

        forms = adapter.list_forms()
        manifest: dict[str, Any] = {
            "app_name": slug,
            "image_tag": image_tag,
            "dialect": dialect.id,
            "dialect_label": dialect.label,
            "init_sql_file": init_filename,
            "include_entity_rows": include_data,
            "forms": [f.name for f in forms],
            "entry_form": "home" if any(f.name == "home" for f in forms) else (forms[0].name if forms else None),
        }
        (app_data_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )

        (bundle_dir / "Dockerfile").write_text(
            _build_dockerfile(dialect.id), encoding="utf-8"
        )
        (bundle_dir / "docker-compose.yml").write_text(
            _build_compose_yml(image_tag, dialect.id), encoding="utf-8"
        )
        (bundle_dir / ".dockerignore").write_text(
            "__pycache__\n*.pyc\n*.pyo\n*.db\ndata/\n", encoding="utf-8"
        )

        zip_path = self._staging_root / f"{slug}_{bundle_id}.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file_path in sorted(bundle_dir.rglob("*")):
                if file_path.is_file():
                    zf.write(file_path, arcname=str(file_path.relative_to(bundle_dir)))

        docker_built = False
        docker_log = "Docker build not requested."
        if build_docker:
            docker_bin = shutil.which("docker")
            if docker_bin is None:
                docker_log = "Docker CLI not found in container/host PATH; zip bundle is ready to build externally."
            else:
                proc = subprocess.run(
                    [docker_bin, "build", "-t", image_tag, "."],
                    cwd=str(bundle_dir),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                docker_built = proc.returncode == 0
                docker_log = (proc.stdout + "\n" + proc.stderr).strip()

        bundle = PackagedAppBundle(
            bundle_id=bundle_id,
            app_name=slug,
            image_tag=image_tag,
            dialect=dialect.id,
            dialect_label=dialect.label,
            bundle_dir=bundle_dir,
            zip_path=zip_path,
            init_sql_filename=init_filename,
            init_sql=init_sql,
            docker_built=docker_built,
            docker_log=docker_log,
        )
        self._bundles[bundle_id] = bundle
        return bundle
