"""Ephemeral Git repository integration for FormSpec Studio."""

from __future__ import annotations

import atexit
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Any

from formspec.db.base import AttributeSpec, WorkspaceAdapter
from formspec.introspect import introspect_entity


class GitSyncError(RuntimeError):
    """Raised when a Git repository operation fails."""


@dataclass(frozen=True)
class GitConfig:
    """Environment-driven Git configuration."""

    repo_url: str | None
    default_branch: str | None
    pat: str | None
    author_name: str = "FormSpec Studio"
    author_email: str = "studio@formspec.local"

    @classmethod
    def from_env(cls) -> "GitConfig":
        repo = (
            os.environ.get("FORMSPEC_GIT_REPO")
            or os.environ.get("GIT_REPO_URL")
            or ""
        ).strip() or None
        branch = (
            os.environ.get("FORMSPEC_GIT_BRANCH")
            or os.environ.get("GIT_BRANCH")
            or ""
        ).strip() or None
        pat = (
            os.environ.get("FORMSPEC_GIT_PAT")
            or os.environ.get("GIT_PAT")
            or os.environ.get("GITHUB_TOKEN")
            or ""
        ).strip() or None
        author_name = (
            os.environ.get("FORMSPEC_GIT_USER_NAME") or "FormSpec Studio"
        ).strip()
        author_email = (
            os.environ.get("FORMSPEC_GIT_USER_EMAIL") or "studio@formspec.local"
        ).strip()
        return cls(
            repo_url=repo,
            default_branch=branch,
            pat=pat,
            author_name=author_name,
            author_email=author_email,
        )

    @property
    def configured(self) -> bool:
        return bool(self.repo_url)

    def safe_repo_display(self) -> str | None:
        if not self.repo_url:
            return None
        return re.sub(r"(https?://)[^/@]+@", r"\1***@", self.repo_url)


class GitWorkspaceManager:
    """Manages an ephemeral clone of the configured Git repository and DB publishing."""

    def __init__(self, config: GitConfig | None = None) -> None:
        self.config = config or GitConfig.from_env()
        self._temp_root: Path | None = None
        self._repo_dir: Path | None = None
        atexit.register(self.cleanup)

    def cleanup(self) -> None:
        if self._temp_root is not None and self._temp_root.exists():
            shutil.rmtree(self._temp_root, ignore_errors=True)
        self._temp_root = None
        self._repo_dir = None

    def _scrub(self, text: str) -> str:
        out = text or ""
        if self.config.pat:
            out = out.replace(self.config.pat, "***")
        return re.sub(r"(https?://)[^/@\s]+@", r"\1***@", out)

    def _run_git(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        askpass_path: Path | None = None

        if self.config.pat:
            fd, raw_path = tempfile.mkstemp(prefix="fs_askpass_", suffix=".sh")
            askpass_path = Path(raw_path)
            os.close(fd)
            # Deliver credentials dynamically via GIT_ASKPASS without storing PAT on remote URL
            askpass_path.write_text(
                "#!/bin/sh\n"
                'case "$1" in\n'
                "  *Username*|*username*) printf '%s\\n' 'x-access-token' ;;\n"
                '  *) printf \'%s\\n\' "$FORMSPEC_GIT_PAT_RUNTIME" ;;\n'
                "esac\n",
                encoding="utf-8",
            )
            os.chmod(askpass_path, stat.S_IRUSR | stat.S_IXUSR)
            env["GIT_ASKPASS"] = str(askpass_path)
            env["FORMSPEC_GIT_PAT_RUNTIME"] = self.config.pat

        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=str(cwd) if cwd else None,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            if askpass_path is not None and askpass_path.exists():
                askpass_path.unlink(missing_ok=True)

        if check and proc.returncode != 0:
            err_msg = self._scrub((proc.stderr or proc.stdout or "Git command failed.").strip())
            raise GitSyncError(err_msg)
        return proc

    @property
    def is_cloned(self) -> bool:
        return self._repo_dir is not None and (self._repo_dir / ".git").exists()

    def ensure_cloned(self) -> dict[str, Any]:
        """Clone the configured remote repo into ephemeral storage on login."""
        if not self.config.configured or not self.config.repo_url:
            return self.status()

        if self.is_cloned and self._repo_dir is not None:
            self._run_git(["fetch", "origin", "--prune"], cwd=self._repo_dir, check=False)
            return self.status()

        self.cleanup()
        self._temp_root = Path(tempfile.mkdtemp(prefix="formspec_git_"))
        target = self._temp_root / "repo"

        clone_cmd = ["clone"]
        if self.config.default_branch:
            clone_cmd.extend(["--branch", self.config.default_branch])
        clone_cmd.extend([self.config.repo_url, str(target)])

        proc = self._run_git(clone_cmd, check=False)
        if proc.returncode != 0 and self.config.default_branch:
            # Fallback to cloning default HEAD if specified branch wasn't on remote yet
            shutil.rmtree(target, ignore_errors=True)
            self._run_git(["clone", self.config.repo_url, str(target)], check=True)
        elif proc.returncode != 0:
            err_msg = self._scrub((proc.stderr or proc.stdout or "Clone failed.").strip())
            self.cleanup()
            raise GitSyncError(err_msg)

        self._repo_dir = target
        return self.status()

    def _current_branch(self) -> str | None:
        if not self.is_cloned or self._repo_dir is None:
            return None
        proc = self._run_git(
            ["rev-parse", "--abbrev-ref", "HEAD"], cwd=self._repo_dir, check=False
        )
        if proc.returncode == 0:
            name = proc.stdout.strip()
            return None if name == "HEAD" else name
        # Empty repo before initial commit
        sym = self._run_git(
            ["symbolic-ref", "--short", "HEAD"], cwd=self._repo_dir, check=False
        )
        return sym.stdout.strip() if sym.returncode == 0 else self.config.default_branch

    def _list_branches(self) -> list[str]:
        if not self.is_cloned or self._repo_dir is None:
            return []
        branches: set[str] = set()
        for ref_prefix in ("refs/heads", "refs/remotes/origin"):
            proc = self._run_git(
                ["for-each-ref", "--format=%(refname:short)", ref_prefix],
                cwd=self._repo_dir,
                check=False,
            )
            if proc.returncode != 0:
                continue
            for line in proc.stdout.splitlines():
                b = line.strip()
                if not b or b == "origin/HEAD" or b == "origin":
                    continue
                if b.startswith("origin/"):
                    b = b[len("origin/") :]
                branches.add(b)
        cur = self._current_branch()
        if cur:
            branches.add(cur)
        return sorted(branches)

    def status(self) -> dict[str, Any]:
        if not self.config.configured:
            return {
                "configured": False,
                "cloned": False,
                "repo_url": None,
                "has_pat": False,
                "current_branch": None,
                "branches": [],
                "dirty_files": [],
            }
        dirty_files: list[str] = []
        if self.is_cloned and self._repo_dir is not None:
            st = self._run_git(["status", "--porcelain"], cwd=self._repo_dir, check=False)
            if st.returncode == 0:
                dirty_files = [l[3:].strip() for l in st.stdout.splitlines() if l.strip()]
        return {
            "configured": True,
            "cloned": self.is_cloned,
            "repo_url": self.config.safe_repo_display(),
            "has_pat": bool(self.config.pat),
            "current_branch": self._current_branch(),
            "branches": self._list_branches(),
            "dirty_files": dirty_files,
        }

    def checkout_branch(self, branch_name: str, *, create: bool = False) -> dict[str, Any]:
        """Switch the ephemeral workspace to `branch_name` (or create it)."""
        clean_branch = branch_name.strip()
        if not clean_branch:
            raise GitSyncError("Branch name cannot be empty.")
        if not self.is_cloned:
            self.ensure_cloned()
        assert self._repo_dir is not None

        self._run_git(["fetch", "origin", "--prune"], cwd=self._repo_dir, check=False)

        local_exists = (
            self._run_git(
                ["show-ref", "--verify", f"refs/heads/{clean_branch}"],
                cwd=self._repo_dir,
                check=False,
            ).returncode
            == 0
        )
        remote_exists = (
            self._run_git(
                ["show-ref", "--verify", f"refs/remotes/origin/{clean_branch}"],
                cwd=self._repo_dir,
                check=False,
            ).returncode
            == 0
        )

        if local_exists:
            self._run_git(["checkout", clean_branch], cwd=self._repo_dir)
            if remote_exists:
                self._run_git(
                    ["merge", "--ff-only", f"origin/{clean_branch}"],
                    cwd=self._repo_dir,
                    check=False,
                )
        elif remote_exists:
            self._run_git(
                ["checkout", "-b", clean_branch, "--track", f"origin/{clean_branch}"],
                cwd=self._repo_dir,
            )
        elif create:
            self._run_git(["checkout", "-b", clean_branch], cwd=self._repo_dir)
        else:
            raise GitSyncError(f"Branch '{clean_branch}' does not exist locally or on origin.")

        return self.status()

    # ------------------------------------------------------------------
    # Repository <-> Database Synchronization
    # ------------------------------------------------------------------

    @staticmethod
    def _write_clean_dir(directory: Path, files: dict[str, str]) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        for existing in directory.iterdir():
            if existing.is_file():
                existing.unlink()
        for rel_name, content in files.items():
            (directory / rel_name).write_text(content, encoding="utf-8")

    def export_db_to_repo(self, adapter: WorkspaceAdapter) -> list[str]:
        """Serialize current workspace entities, RBAC, forms, actions & modules into repo."""
        if not self.is_cloned:
            self.ensure_cloned()
        if self._repo_dir is None:
            raise GitSyncError("Git repository is not configured.")

        rbac = adapter.list_rbac_snapshot()
        schema_doc = {
            "entities": [
                {
                    "name": e["name"],
                    "label": e["label"],
                    "description": e.get("description"),
                    "columns": [c for c in e.get("columns", []) if not c.get("is_pk")],
                }
                for e in adapter.list_domain_entities()
            ],
            "rbac": {
                "roles": rbac.roles,
                "permissions": rbac.permissions,
                "grants": rbac.grants,
            },
        }
        (self._repo_dir / "schema.json").write_text(
            json.dumps(schema_doc, indent=2) + "\n", encoding="utf-8"
        )

        # Forms
        forms_files: dict[str, str] = {}
        forms_manifest: dict[str, dict[str, Any]] = {}
        for f in adapter.list_forms():
            forms_files[f"{f.name}.fspec"] = f.fspec_source
            forms_manifest[f.name] = {
                "label": f.label,
                "description": f.description,
                "entity_name": f.entity_name,
                "form_type": f.form_type,
                "formatter": f.formatter,
                "theme": f.theme,
            }
        forms_files["manifest.json"] = json.dumps(forms_manifest, indent=2) + "\n"
        self._write_clean_dir(self._repo_dir / "forms", forms_files)

        # Server Actions
        sa_files: dict[str, str] = {}
        sa_manifest: dict[str, dict[str, Any]] = {}
        for sa in adapter.list_server_actions():
            ext = ".py" if sa.target_runtime == "python" else ".mjs"
            sa_files[f"{sa.name}{ext}"] = sa.code
            sa_manifest[sa.name] = {
                "label": sa.label,
                "description": sa.description,
                "entity_name": sa.entity_name,
                "action_type": sa.action_type,
                "target_runtime": sa.target_runtime,
                "filename": f"{sa.name}{ext}",
            }
        sa_files["manifest.json"] = json.dumps(sa_manifest, indent=2) + "\n"
        self._write_clean_dir(self._repo_dir / "server_actions", sa_files)

        # Client Actions
        ca_files: dict[str, str] = {}
        ca_manifest: dict[str, dict[str, Any]] = {}
        for ca in adapter.list_client_actions():
            ca_files[f"{ca.name}.mjs"] = ca.code
            ca_manifest[ca.name] = {
                "label": ca.label,
                "description": ca.description,
                "target_form_name": ca.target_form_name,
                "action_type": ca.action_type,
                "params_spec": ca.params_spec,
            }
        ca_files["manifest.json"] = json.dumps(ca_manifest, indent=2) + "\n"
        self._write_clean_dir(self._repo_dir / "client_actions", ca_files)

        # Server Modules
        sm_files: dict[str, str] = {}
        sm_manifest: dict[str, dict[str, Any]] = {}
        for sm in adapter.list_server_modules():
            sm_files[f"{sm.name}.mjs"] = sm.code
            sm_manifest[sm.name] = {
                "label": sm.label,
                "description": sm.description,
                "target_runtime": sm.target_runtime,
            }
        sm_files["manifest.json"] = json.dumps(sm_manifest, indent=2) + "\n"
        self._write_clean_dir(self._repo_dir / "server_modules", sm_files)

        # Client Modules
        cm_files: dict[str, str] = {}
        cm_manifest: dict[str, dict[str, Any]] = {}
        for cm in adapter.list_client_modules():
            cm_files[f"{cm.name}.mjs"] = cm.code
            cm_manifest[cm.name] = {
                "label": cm.label,
                "description": cm.description,
            }
        cm_files["manifest.json"] = json.dumps(cm_manifest, indent=2) + "\n"
        self._write_clean_dir(self._repo_dir / "client_modules", cm_files)

        return [
            "schema.json",
            "forms/*",
            "server_actions/*",
            "client_actions/*",
            "server_modules/*",
            "client_modules/*",
        ]

    @staticmethod
    def _topological_entities(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
        remaining = {str(e["name"]): e for e in entities}
        ordered: list[dict[str, Any]] = []
        placed: set[str] = set()
        while remaining:
            progress = False
            for name in sorted(list(remaining.keys())):
                ent = remaining[name]
                deps = {
                    str(c["fk_table"])
                    for c in ent.get("columns", [])
                    if c.get("fk_table") and str(c["fk_table"]) != name
                }
                if deps.issubset(placed) or not deps.intersection(remaining):
                    ordered.append(ent)
                    placed.add(name)
                    remaining.pop(name)
                    progress = True
            if not progress:
                for name in sorted(list(remaining.keys())):
                    ordered.append(remaining.pop(name))
        return ordered

    def publish_branch_to_db(self, adapter: WorkspaceAdapter) -> dict[str, Any]:
        """Apply files from the checked-out Git branch into the application database."""
        if not self.is_cloned:
            self.ensure_cloned()
        if self._repo_dir is None:
            raise GitSyncError("Git repository is not configured.")

        counts = {
            "entities": 0,
            "server_modules": 0,
            "client_modules": 0,
            "forms": 0,
            "server_actions": 0,
            "client_actions": 0,
        }

        # 1. Domain entities & RBAC (schema.json)
        schema_path = self._repo_dir / "schema.json"
        if schema_path.is_file():
            schema_doc = json.loads(schema_path.read_text(encoding="utf-8"))
            existing_entities = {e["name"]: e for e in adapter.list_domain_entities()}
            for ent in self._topological_entities(schema_doc.get("entities", [])):
                ename = str(ent["name"])
                elabel = str(ent.get("label") or ename.replace("_", " ").title())
                edesc = ent.get("description")
                attrs = [
                    AttributeSpec(
                        name=str(c["name"]),
                        fspec_type=str(c.get("fspec_type", "text")),
                        nullable=bool(c.get("nullable", True)),
                        default=c.get("default"),
                        fk_table=c.get("fk_table"),
                    )
                    for c in ent.get("columns", [])
                    if not c.get("is_pk")
                ]
                if ename not in existing_entities:
                    adapter.create_entity(ename, elabel, edesc, attrs)
                else:
                    cur_cols = {c["name"] for c in existing_entities[ename].get("columns", [])}
                    for spec in attrs:
                        if spec.name not in cur_cols:
                            adapter.add_attribute(ename, spec)
                counts["entities"] += 1

            rbac_doc = schema_doc.get("rbac", {})
            for role_name in rbac_doc.get("roles", []):
                adapter.create_role(str(role_name))
            for perm_name in rbac_doc.get("permissions", []):
                adapter.ensure_permission(str(perm_name))
            for role_name, perm_list in rbac_doc.get("grants", {}).items():
                adapter.create_role(str(role_name))
                for perm_name in perm_list:
                    adapter.set_role_permission(str(role_name), str(perm_name), True)

        # 2. Server Modules
        sm_dir = self._repo_dir / "server_modules"
        if sm_dir.is_dir():
            manifest = (
                json.loads((sm_dir / "manifest.json").read_text(encoding="utf-8"))
                if (sm_dir / "manifest.json").is_file()
                else {}
            )
            for mod_file in sorted(sm_dir.glob("*.mjs")):
                name = mod_file.stem
                meta = manifest.get(name, {})
                adapter.save_server_module(
                    name=name,
                    label=str(meta.get("label") or name.replace("_", " ").title()),
                    code=mod_file.read_text(encoding="utf-8"),
                    description=meta.get("description"),
                    target_runtime=str(meta.get("target_runtime") or "node"),
                )
                counts["server_modules"] += 1

        # 3. Client Modules
        cm_dir = self._repo_dir / "client_modules"
        if cm_dir.is_dir():
            manifest = (
                json.loads((cm_dir / "manifest.json").read_text(encoding="utf-8"))
                if (cm_dir / "manifest.json").is_file()
                else {}
            )
            for mod_file in sorted(cm_dir.glob("*.mjs")):
                name = mod_file.stem
                meta = manifest.get(name, {})
                adapter.save_client_module(
                    name=name,
                    label=str(meta.get("label") or name.replace("_", " ").title()),
                    code=mod_file.read_text(encoding="utf-8"),
                    description=meta.get("description"),
                )
                counts["client_modules"] += 1

        # 4. Forms
        forms_dir = self._repo_dir / "forms"
        if forms_dir.is_dir():
            manifest = (
                json.loads((forms_dir / "manifest.json").read_text(encoding="utf-8"))
                if (forms_dir / "manifest.json").is_file()
                else {}
            )
            for fspec_file in sorted(forms_dir.glob("*.fspec")):
                name = fspec_file.stem
                meta = manifest.get(name, {})
                adapter.save_form(
                    name=name,
                    label=str(meta.get("label") or name.replace("_", " ").title()),
                    fspec_source=fspec_file.read_text(encoding="utf-8"),
                    entity_name=meta.get("entity_name"),
                    description=meta.get("description"),
                    formatter=str(meta.get("formatter") or "html5"),
                    theme=str(meta.get("theme") or "default"),
                    form_type=str(meta.get("form_type") or "detail"),
                )
                counts["forms"] += 1

        # 5. Server Actions
        sa_dir = self._repo_dir / "server_actions"
        if sa_dir.is_dir():
            manifest = (
                json.loads((sa_dir / "manifest.json").read_text(encoding="utf-8"))
                if (sa_dir / "manifest.json").is_file()
                else {}
            )
            for sa_file in sorted([*sa_dir.glob("*.mjs"), *sa_dir.glob("*.py")]):
                name = sa_file.stem
                meta = manifest.get(name, {})
                runtime = meta.get("target_runtime") or (
                    "python" if sa_file.suffix == ".py" else "node"
                )
                adapter.save_server_action(
                    name=name,
                    label=str(meta.get("label") or name.replace("_", " ").title()),
                    code=sa_file.read_text(encoding="utf-8"),
                    entity_name=meta.get("entity_name"),
                    description=meta.get("description"),
                    action_type=str(meta.get("action_type") or "upsert"),
                    target_runtime=str(runtime),
                )
                counts["server_actions"] += 1

        # 6. Client Actions (after forms so target_form_name resolves to form ID)
        ca_dir = self._repo_dir / "client_actions"
        if ca_dir.is_dir():
            manifest = (
                json.loads((ca_dir / "manifest.json").read_text(encoding="utf-8"))
                if (ca_dir / "manifest.json").is_file()
                else {}
            )
            for ca_file in sorted(ca_dir.glob("*.mjs")):
                name = ca_file.stem
                meta = manifest.get(name, {})
                adapter.save_client_action(
                    name=name,
                    label=str(meta.get("label") or name.replace("_", " ").title()),
                    target_form_name=meta.get("target_form_name"),
                    description=meta.get("description"),
                    action_type=str(meta.get("action_type") or "link"),
                    params_spec=meta.get("params_spec"),
                    code=ca_file.read_text(encoding="utf-8"),
                )
                counts["client_actions"] += 1

        return {"branch": self._current_branch(), "published": counts}

    def commit_and_push(
        self,
        adapter: WorkspaceAdapter,
        *,
        message: str = "Update FormSpec workspace",
    ) -> dict[str, Any]:
        """Export current DB state to ephemeral branch clone, commit, and push via PAT."""
        if not self.is_cloned:
            self.ensure_cloned()
        if self._repo_dir is None:
            raise GitSyncError("Git repository is not configured.")

        self.export_db_to_repo(adapter)
        branch = self._current_branch() or self.config.default_branch or "main"

        self._run_git(["add", "-A"], cwd=self._repo_dir)
        st = self._run_git(["status", "--porcelain"], cwd=self._repo_dir, check=False)
        committed = False
        if st.stdout.strip():
            commit_msg = message.strip() or "Update FormSpec workspace"
            self._run_git(
                [
                    "-c",
                    f"user.name={self.config.author_name}",
                    "-c",
                    f"user.email={self.config.author_email}",
                    "commit",
                    "-m",
                    commit_msg,
                ],
                cwd=self._repo_dir,
            )
            committed = True

        self._run_git(["push", "-u", "origin", branch], cwd=self._repo_dir)
        return {
            "pushed": True,
            "committed": committed,
            "branch": branch,
            "status": self.status(),
        }
