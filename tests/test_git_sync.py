"""Integration tests for Git repository sync (ephemeral clone, branch switch, publish, push)."""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from formspec.ide.git_sync import GitConfig, GitSyncError, GitWorkspaceManager
from formspec.ide.server import IDEApp


def _extract_token(set_cookie_header: str) -> str:
    return set_cookie_header.split(";", 1)[0].split("=", 1)[1]


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _create_bare_origin_with_branches(root: Path) -> Path:
    """Create a local bare remote repo with `main` and `feature/hr` branches."""
    bare = root / "remote.git"
    bare.mkdir()
    _git(["init", "--bare", "--initial-branch=main"], bare)

    seed = root / "seed_work"
    _git(["clone", str(bare), str(seed)], root)
    _git(["config", "user.name", "Tester"], seed)
    _git(["config", "user.email", "tester@example.com"], seed)

    (seed / "forms").mkdir()
    (seed / "forms" / "welcome.fspec").write_text(
        '::: form welcome "Welcome Branch" @(admin:write, *:read)\n\t# Hello Main\n',
        encoding="utf-8",
    )
    _git(["add", "-A"], seed)
    _git(["commit", "-m", "Initial main commit"], seed)
    _git(["push", "-u", "origin", "main"], seed)

    # Create feature/hr branch with an entity + form + module
    _git(["checkout", "-b", "feature/hr"], seed)
    (seed / "schema.json").write_text(
        '{\n'
        '  "entities": [\n'
        '    {"name": "role_note", "label": "Role Note", "columns": [{"name": "title", "fspec_type": "text", "nullable": false}]}\n'
        "  ],\n"
        '  "rbac": {"roles": ["admin", "guest", "anon"], "permissions": ["role_note:read", "role_note:write"], "grants": {"admin": ["role_note:read", "role_note:write"], "guest": ["role_note:read"]}}\n'
        "}\n",
        encoding="utf-8",
    )
    (seed / "forms" / "welcome.fspec").write_text(
        '::: form welcome "HR Welcome" @(admin:write, guest:read, *:none)\n\t# Hello Feature HR\n',
        encoding="utf-8",
    )
    _git(["add", "-A"], seed)
    _git(["commit", "-m", "Add feature/hr workspace"], seed)
    _git(["push", "-u", "origin", "feature/hr"], seed)
    return bare


class GitSyncTests(unittest.TestCase):
    def test_ephemeral_clone_on_login_branch_switch_publish_and_push(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            bare_remote = _create_bare_origin_with_branches(tmp_dir)
            db_path = tmp_dir / "studio_git.db"

            git_cfg = GitConfig(
                repo_url=str(bare_remote),
                default_branch="main",
                pat="secret_pat_token_123",
            )
            app = IDEApp(dsn=f"sqlite:///{db_path}", secret="git-sec", git_config=git_cfg)
            try:
                self.assertFalse(app.git.is_cloned)

                # Setup superuser clones repo into ephemeral storage on login/bootstrap
                code, setup_res, cookie = app.handle_api(
                    "POST",
                    "/api/auth/setup",
                    {"username": "superuser", "password": "strong-password-123"},
                )
                self.assertEqual(code, 201)
                self.assertTrue(setup_res["git"]["cloned"])
                self.assertEqual(setup_res["git"]["current_branch"], "main")
                self.assertEqual(
                    set(setup_res["git"]["branches"]), {"feature/hr", "main"}
                )
                self.assertNotIn("secret_pat_token_123", repr(setup_res))

                token = _extract_token(cookie or "")

                # Switch branch to feature/hr via API
                code, chk_res, _ = app.handle_api(
                    "POST",
                    "/api/git/checkout",
                    {"branch": "feature/hr"},
                    session_token=token,
                )
                self.assertEqual(code, 200)
                self.assertEqual(chk_res["git"]["current_branch"], "feature/hr")

                # Publish feature/hr branch contents to DB
                code, pub_res, _ = app.handle_api(
                    "POST", "/api/git/publish", {}, session_token=token
                )
                self.assertEqual(code, 200)
                self.assertGreaterEqual(pub_res["result"]["published"]["entities"], 1)
                self.assertGreaterEqual(pub_res["result"]["published"]["forms"], 1)

                _, ents, _ = app.handle_api("GET", "/api/entities", session_token=token)
                self.assertIn("role_note", [e["name"] for e in ents["entities"]])
                _, forms, _ = app.handle_api("GET", "/api/forms", session_token=token)
                welcome = next(f for f in forms["forms"] if f["name"] == "welcome")
                self.assertIn("# Hello Feature HR", welcome["fspec_source"])

                # Edit workspace in DB and push back to bare origin on branch feature/hr
                app.handle_api(
                    "PUT",
                    f"/api/forms/{welcome['id']}",
                    {"fspec_source": '::: form welcome "Updated HR"\n\t# Updated From IDE\n'},
                    session_token=token,
                )
                code, push_res, _ = app.handle_api(
                    "POST",
                    "/api/git/push",
                    {"message": "Push updated welcome form"},
                    session_token=token,
                )
                self.assertEqual(code, 200)
                self.assertTrue(push_res["pushed"])
                self.assertTrue(push_res["committed"])
                self.assertEqual(push_res["branch"], "feature/hr")

                # Verify remote origin now contains exported workspace files & updated form
                verify_clone = tmp_dir / "verify_remote"
                _git(["clone", "--branch", "feature/hr", str(bare_remote), str(verify_clone)], tmp_dir)
                self.assertIn(
                    "# Updated From IDE",
                    (verify_clone / "forms" / "welcome.fspec").read_text(encoding="utf-8"),
                )
                self.assertTrue((verify_clone / "schema.json").is_file())
                self.assertTrue((verify_clone / "server_modules" / "formspec_server.mjs").is_file())
                self.assertTrue((verify_clone / "client_modules" / "formspec_header.mjs").is_file())

                # Logout cleans up ephemeral clone; re-login clones fresh again
                app.handle_api("POST", "/api/auth/logout", session_token=token)
                self.assertFalse(app.git.is_cloned)

                code, login_res, _ = app.handle_api(
                    "POST",
                    "/api/auth/login",
                    {"username": "superuser", "password": "strong-password-123"},
                )
                self.assertEqual(code, 200)
                self.assertTrue(login_res["git"]["cloned"])
            finally:
                app.git.cleanup()
                app.packager.cleanup()

    def test_pat_scrubbing_and_safe_repo_url(self) -> None:
        cfg = GitConfig(
            repo_url="https://user:super_secret_pat@github.com/org/repo.git",
            default_branch="main",
            pat="another_pat_xyz",
        )
        mgr = GitWorkspaceManager(cfg)
        try:
            self.assertEqual(cfg.safe_repo_display(), "https://***@github.com/org/repo.git")
            scrubbed = mgr._scrub(
                "fatal: https://user:super_secret_pat@github.com token another_pat_xyz failed"
            )
            self.assertNotIn("super_secret_pat", scrubbed)
            self.assertNotIn("another_pat_xyz", scrubbed)
            with self.assertRaises(GitSyncError):
                mgr.checkout_branch("   ")
        finally:
            mgr.cleanup()


if __name__ == "__main__":
    unittest.main()
