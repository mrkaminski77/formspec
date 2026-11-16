"""Pluggable authentication providers for the packaged FormSpec web application."""

from __future__ import annotations

from abc import ABC, abstractmethod
import base64
from dataclasses import dataclass
import json
import os
from typing import Any, Mapping

from formspec.db.base import WorkspaceAdapter
from formspec.ide.auth import SessionManager, verify_password


@dataclass(frozen=True)
class AuthUser:
    """Resolved request identity and RBAC role list for the packaged runtime."""

    id: int | None
    username: str
    is_superuser: bool
    roles: list[str]
    authenticated: bool = False
    provider: str = "local"

    @property
    def effective_role(self) -> str:
        if self.is_superuser:
            return "superuser"
        if self.roles:
            return self.roles[0]
        return "anon"


def _anon_user(provider: str = "local") -> AuthUser:
    return AuthUser(
        id=None,
        username="anon",
        is_superuser=False,
        roles=["anon"],
        authenticated=False,
        provider=provider,
    )


class AuthProvider(ABC):
    """Abstract authentication provider supporting local DB or external IdPs (e.g. EasyAuth)."""

    id: str
    supports_login_form: bool = True

    @abstractmethod
    def authenticate_request(
        self,
        adapter: WorkspaceAdapter,
        headers: Mapping[str, str],
        session_token: str | None,
    ) -> AuthUser:
        """Resolve the current `AuthUser` (falling back to `anon` if allowed)."""

    def login(
        self,
        adapter: WorkspaceAdapter,
        username: str,
        password: str,
    ) -> tuple[AuthUser, str | None]:
        """Perform credential login if supported by this provider."""
        raise NotImplementedError(f"Auth provider '{self.id}' does not support password login.")


class LocalDatabaseAuthProvider(AuthProvider):
    """Cookie-session authentication against the `users` table with `anon` fallback."""

    id = "local"
    supports_login_form = True

    def __init__(self, secret: str | None = None) -> None:
        self.sessions = SessionManager(secret=secret)

    def authenticate_request(
        self,
        adapter: WorkspaceAdapter,
        headers: Mapping[str, str],
        session_token: str | None,
    ) -> AuthUser:
        uid = self.sessions.verify_token(session_token)
        if uid is not None:
            user = adapter.get_user_by_id(uid)
            if user is not None:
                return AuthUser(
                    id=user.id,
                    username=user.username,
                    is_superuser=user.is_superuser,
                    roles=user.roles or ["guest"],
                    authenticated=True,
                    provider=self.id,
                )
        return _anon_user(self.id)

    def login(
        self,
        adapter: WorkspaceAdapter,
        username: str,
        password: str,
    ) -> tuple[AuthUser, str | None]:
        user = adapter.get_user_by_username(username.strip())
        if user is None or not verify_password(password, user.password_hash):
            raise ValueError("Invalid username or password.")
        token = self.sessions.create_token(user.id)
        set_cookie = (
            f"{SessionManager.COOKIE_NAME}={token}; HttpOnly; Path=/; SameSite=Lax"
        )
        return (
            AuthUser(
                id=user.id,
                username=user.username,
                is_superuser=user.is_superuser,
                roles=user.roles or ["guest"],
                authenticated=True,
                provider=self.id,
            ),
            set_cookie,
        )


class AzureEasyAuthProvider(AuthProvider):
    """Azure App Service EasyAuth (`X-MS-CLIENT-PRINCIPAL` / `-NAME`) identity provider."""

    id = "easyauth"
    supports_login_form = False

    def __init__(self, secret: str | None = None) -> None:
        self._fallback_local = LocalDatabaseAuthProvider(secret=secret)

    def authenticate_request(
        self,
        adapter: WorkspaceAdapter,
        headers: Mapping[str, str],
        session_token: str | None,
    ) -> AuthUser:
        # Case-insensitive header lookup
        lower = {k.lower(): v for k, v in headers.items()}
        raw_principal = lower.get("x-ms-client-principal")
        principal_name = lower.get("x-ms-client-principal-name")

        claims_roles: list[str] = []
        if raw_principal:
            try:
                decoded = base64.b64decode(raw_principal).decode("utf-8")
                payload: dict[str, Any] = json.loads(decoded)
                principal_name = (
                    principal_name
                    or payload.get("userDetails")
                    or payload.get("name")
                )
                for r in payload.get("userRoles", []) or []:
                    if r and str(r).lower() not in ("anonymous", "authenticated"):
                        claims_roles.append(str(r))
                for c in payload.get("claims", []) or []:
                    typ = str(c.get("typ") or "").lower()
                    if typ in ("roles", "role") or typ.endswith("/role"):
                        val = str(c.get("val") or "").strip()
                        if val:
                            claims_roles.append(val)
            except Exception:
                principal_name = principal_name or None

        if principal_name:
            db_user = adapter.get_user_by_username(principal_name.strip())
            if db_user is not None:
                merged_roles = list(dict.fromkeys([*(db_user.roles or []), *claims_roles]))
                return AuthUser(
                    id=db_user.id,
                    username=db_user.username,
                    is_superuser=db_user.is_superuser,
                    roles=merged_roles or ["guest"],
                    authenticated=True,
                    provider=self.id,
                )
            return AuthUser(
                id=None,
                username=principal_name.strip(),
                is_superuser=False,
                roles=list(dict.fromkeys(claims_roles)) or ["guest"],
                authenticated=True,
                provider=self.id,
            )

        local = self._fallback_local.authenticate_request(adapter, headers, session_token)
        if local.authenticated:
            return local
        return _anon_user(self.id)


_PROVIDER_REGISTRY: dict[str, type[AuthProvider]] = {}


def register_auth_provider(provider_id: str, cls: type[AuthProvider]) -> None:
    """Register a pluggable AuthProvider class."""
    _PROVIDER_REGISTRY[provider_id.strip().lower()] = cls


def get_auth_provider(provider_id: str | None = None, *, secret: str | None = None) -> AuthProvider:
    key = (provider_id or os.environ.get("FORMSPEC_AUTH_PROVIDER") or "local").strip().lower()
    if key not in _PROVIDER_REGISTRY:
        supported = ", ".join(sorted(_PROVIDER_REGISTRY))
        raise ValueError(f"Unknown auth provider '{key}'. Supported: {supported}.")
    cls = _PROVIDER_REGISTRY[key]
    return cls(secret=secret)  # type: ignore[call-arg]


def list_auth_providers() -> list[str]:
    return sorted(_PROVIDER_REGISTRY)


register_auth_provider("local", LocalDatabaseAuthProvider)
register_auth_provider("easyauth", AzureEasyAuthProvider)
