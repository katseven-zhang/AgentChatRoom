"""Persistent OAuth 2.1 provider for explicitly approved remote coordinators.

SDK handlers validate redirect URIs and S256 PKCE. This provider owns approval,
atomic single-use grants, token rotation, expiry and revocation. Database work
always runs off the ASGI event loop; no OAuth secret is stored in clear text.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from functools import partial
from typing import Any
from urllib.parse import urlencode, urlparse

from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
    RefreshToken, RegistrationError, TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .contracts import REMOTE_COORDINATOR_SCOPES
from .errors import DomainError
from .services import iso_now, new_id


def secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class RemoteOAuthProvider:
    def __init__(self, service: Any) -> None:
        self.service = service
        self.database = service.database
        self.settings = service.settings
        self.issuer = self.settings.remote_mcp_public_base_url.rstrip("/")
        self.resource = self.issuer + "/mcp"

    async def _thread(self, function: Any, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(partial(function, *args, **kwargs))

    def _audit(self, connection: Any, client_id: str, kind: str, **payload: Any) -> None:
        connection.execute(
            "INSERT INTO remote_oauth_events(id,client_id,event_type,payload_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (new_id("oauth_event"), client_id, kind, json.dumps(payload), iso_now()),
        )

    def client_record(self, client_id: str, *, active: bool = True) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM remote_oauth_clients WHERE client_id = ?", (client_id,),
            ).fetchone()
        if row is None or (active and row["revoked_at"]):
            return None
        return dict(row)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = await self._thread(self.client_record, client_id)
        return OAuthClientInformationFull.model_validate_json(row["metadata_json"]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await self._thread(self._register_client, client_info)

    def _register_client(self, client: OAuthClientInformationFull) -> None:
        if client.token_endpoint_auth_method != "none":
            raise RegistrationError("invalid_client_metadata", "Use public-client authentication: none, with S256 PKCE")
        name = (client.client_name or "Remote coordinator").strip()
        metadata = client.model_dump_json()
        if not 1 <= len(name) <= 128 or len(metadata.encode()) > 16384 or not 1 <= len(client.redirect_uris) <= 10:
            raise RegistrationError("invalid_client_metadata", "Client metadata is too large or incomplete")
        for value in client.redirect_uris:
            url = urlparse(str(value))
            if url.username or url.password or url.fragment or not (
                url.scheme == "https" or (url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"})
            ):
                raise RegistrationError("invalid_redirect_uri", "Use HTTPS callbacks or loopback HTTP callbacks")
        with self.database.connect(write=True) as connection:
            count = connection.execute("SELECT COUNT(*) AS n FROM remote_oauth_clients WHERE revoked_at IS NULL").fetchone()["n"]
            if int(count) >= self.settings.remote_oauth_max_clients:
                raise RegistrationError("invalid_client_metadata", "Client limit reached; ask the local operator to revoke unused connections")
            connection.execute(
                "INSERT INTO remote_oauth_clients(client_id,metadata_json,software_key,display_name,created_at) VALUES(?,?,?,?,?)",
                (client.client_id, metadata, new_id("remote_software"), name, iso_now()),
            )
            self._audit(connection, client.client_id or "", "client.registered")

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        return await self._thread(self._authorize, client, params)

    def _authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource != self.resource:
            raise AuthorizeError("invalid_request", "resource must match the advertised MCP resource")
        scopes = params.scopes or list(REMOTE_COORDINATOR_SCOPES)
        if not set(scopes).issubset(REMOTE_COORDINATOR_SCOPES) or "room:read" not in scopes:
            raise AuthorizeError("invalid_scope", "Request only the advertised coordinator scopes, including room:read")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge):
            raise AuthorizeError("invalid_request", "A valid S256 PKCE challenge is required")
        params.scopes = scopes
        ticket = secrets.token_urlsafe(32)
        request_id = new_id("oauth_request")
        with self.database.connect(write=True) as connection:
            row = connection.execute("SELECT revoked_at FROM remote_oauth_clients WHERE client_id=?", (client.client_id,)).fetchone()
            if row is None or row["revoked_at"]:
                raise AuthorizeError("unauthorized_client", "This client is revoked or unknown")
            count = connection.execute(
                "SELECT COUNT(*) AS n FROM remote_oauth_authorizations WHERE status IN ('pending','approved') AND expires_at>?",
                (int(time.time()),),
            ).fetchone()["n"]
            if int(count) >= self.settings.remote_oauth_max_pending:
                raise AuthorizeError("temporarily_unavailable", "Pending authorization limit reached")
            connection.execute(
                "INSERT INTO remote_oauth_authorizations(id,client_id,ticket_hash,verification_code,params_json,status,expires_at,created_at) "
                "VALUES(?,?,?,?,?,'pending',?,?)",
                (request_id, client.client_id, secret_hash(ticket), f"{secrets.randbelow(1000000):06d}",
                 params.model_dump_json(), int(time.time()) + self.settings.remote_oauth_authorization_ttl_seconds, iso_now()),
            )
            self._audit(connection, client.client_id or "", "authorization.requested", request_id=request_id, scopes=scopes)
        return self.issuer + "/oauth/consent?" + urlencode({"request_id": request_id, "ticket": ticket})

    def status(self) -> dict[str, Any]:
        with self.database.connect() as connection:
            requests = connection.execute(
                "SELECT a.*, c.display_name, c.metadata_json FROM remote_oauth_authorizations a "
                "JOIN remote_oauth_clients c ON c.client_id=a.client_id "
                "WHERE a.status='pending' AND a.expires_at>? AND c.revoked_at IS NULL ORDER BY a.created_at",
                (int(time.time()),),
            ).fetchall()
            clients = connection.execute(
                "SELECT client_id,display_name,created_at,approved_at,revoked_at FROM remote_oauth_clients "
                "ORDER BY created_at DESC LIMIT ?", (self.settings.remote_oauth_max_clients,),
            ).fetchall()
        return {
            "enabled": self.settings.remote_mcp_enabled, "mcp_url": self.resource,
            "scopes": list(REMOTE_COORDINATOR_SCOPES), "clients": [dict(row) for row in clients],
            "pending": [{
                "request_id": row["id"], "client_id": row["client_id"], "display_name": row["display_name"],
                "verification_code": row["verification_code"], "expires_at": row["expires_at"],
                "redirect_uri": json.loads(row["params_json"])["redirect_uri"],
                "scopes": json.loads(row["params_json"])["scopes"],
            } for row in requests],
        }

    def decide(self, request_id: str, *, approve: bool, verification_code: str, display_name: str = "") -> dict[str, Any]:
        with self.database.connect(write=True) as connection:
            row = connection.execute("SELECT * FROM remote_oauth_authorizations WHERE id=?", (request_id,)).fetchone()
            if row is None or row["status"] != "pending" or int(row["expires_at"]) <= time.time():
                raise DomainError("oauth_request_not_pending", "Authorization is no longer pending", status_code=409)
            if approve and not hmac.compare_digest(str(row["verification_code"]), verification_code):
                raise DomainError("oauth_verification_code_mismatch", "Match the code shown in the OAuth window", status_code=403)
            client = connection.execute("SELECT * FROM remote_oauth_clients WHERE client_id=?", (row["client_id"],)).fetchone()
            if client is None or client["revoked_at"]:
                raise DomainError("oauth_client_revoked", "The client has been revoked", status_code=409)
            name = display_name.strip() or str(client["display_name"])
            if not 1 <= len(name) <= 128 or any(ord(ch) < 32 for ch in name):
                raise DomainError("invalid_remote_display_name", "Use a display name between 1 and 128 characters")
            if client["approved_at"] and name != client["display_name"]:
                raise DomainError("remote_identity_name_fixed", "A connected client's durable name cannot be changed during reauthorization")
            now = iso_now()
            connection.execute("UPDATE remote_oauth_authorizations SET status=?,decided_at=? WHERE id=?",
                               ("approved" if approve else "denied", now, request_id))
            if approve:
                connection.execute("UPDATE remote_oauth_clients SET approved_at=?,display_name=? WHERE client_id=?",
                                   (now, name, row["client_id"]))
            self._audit(connection, row["client_id"], "authorization.approved" if approve else "authorization.denied", request_id=request_id)
        return {"status": "approved" if approve else "denied", "request_id": request_id}

    def consent(self, request_id: str, ticket: str, *, complete: bool = False) -> dict[str, Any]:
        with self.database.connect(write=complete) as connection:
            row = connection.execute(
                "SELECT a.*, c.display_name, c.revoked_at FROM remote_oauth_authorizations a "
                "JOIN remote_oauth_clients c ON c.client_id=a.client_id WHERE a.id=?", (request_id,),
            ).fetchone()
            if row is None or not hmac.compare_digest(row["ticket_hash"], secret_hash(ticket)):
                raise DomainError("oauth_request_unknown", "Authorization request not found", status_code=404)
            params = AuthorizationParams.model_validate_json(row["params_json"])
            if row["revoked_at"] or int(row["expires_at"]) <= time.time():
                return {"status": "expired"}
            if row["status"] == "denied" and complete:
                query = {"error": "access_denied"}
                if params.state is not None:
                    query["state"] = params.state
                return {"redirect": self._redirect(str(params.redirect_uri), query)}
            if row["status"] == "approved" and complete:
                code = secrets.token_urlsafe(32)
                changed = connection.execute(
                    "UPDATE remote_oauth_authorizations SET status='code_issued',code_hash=?,code_expires_at=? WHERE id=? AND status='approved'",
                    (secret_hash(code), int(row["expires_at"]), request_id),
                )
                if changed.rowcount != 1:
                    raise DomainError("oauth_request_already_completed", "Authorization has already completed", status_code=409)
                query = {"code": code}
                if params.state is not None:
                    query["state"] = params.state
                return {"redirect": self._redirect(str(params.redirect_uri), query)}
            return {"status": row["status"], "verification_code": row["verification_code"],
                    "display_name": row["display_name"], "scopes": params.scopes, "redirect_uri": str(params.redirect_uri)}

    @staticmethod
    def _redirect(url: str, values: dict[str, str]) -> str:
        return url + ("&" if "?" in url else "?") + urlencode(values)

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        return await self._thread(self._load_code, client, authorization_code)

    def _load_code(self, client: OAuthClientInformationFull, code: str) -> AuthorizationCode | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM remote_oauth_authorizations WHERE code_hash=? AND client_id=? AND status='code_issued'",
                (secret_hash(code), client.client_id),
            ).fetchone()
        if row is None or int(row["code_expires_at"] or 0) <= time.time():
            return None
        params = AuthorizationParams.model_validate_json(row["params_json"])
        return AuthorizationCode(code=code, client_id=client.client_id or "", scopes=params.scopes or [],
            expires_at=int(row["code_expires_at"]), code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri, redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource, subject=client.client_id)

    def _mint(self, connection: Any, client_id: str, grant_id: str, scopes: list[str]) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        for secret, kind, ttl in (
            (access, "access", self.settings.remote_oauth_access_ttl_seconds),
            (refresh, "refresh", self.settings.remote_oauth_refresh_ttl_seconds),
        ):
            connection.execute(
                "INSERT INTO remote_oauth_tokens(token_hash,client_id,grant_id,kind,scopes_json,resource_url,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (secret_hash(secret), client_id, grant_id, kind, json.dumps(scopes), self.resource, int(time.time()) + ttl, iso_now()),
            )
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=self.settings.remote_oauth_access_ttl_seconds,
                          refresh_token=refresh, scope=" ".join(scopes))

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        return await self._thread(self._exchange_code, client, authorization_code)

    def _exchange_code(self, client: OAuthClientInformationFull, code: AuthorizationCode) -> OAuthToken:
        with self.database.connect(write=True) as connection:
            row = connection.execute(
                "SELECT a.* FROM remote_oauth_authorizations a JOIN remote_oauth_clients c ON c.client_id=a.client_id "
                "WHERE a.code_hash=? AND a.client_id=? AND a.status='code_issued' AND c.revoked_at IS NULL",
                (secret_hash(code.code), client.client_id),
            ).fetchone()
            if row is None or int(row["code_expires_at"] or 0) <= time.time():
                raise TokenError("invalid_grant", "Authorization code is expired or already used")
            changed = connection.execute("UPDATE remote_oauth_authorizations SET status='consumed' WHERE id=? AND status='code_issued'", (row["id"],))
            if changed.rowcount != 1:
                raise TokenError("invalid_grant", "Authorization code is already used")
            self._audit(connection, client.client_id or "", "authorization.consumed", request_id=row["id"])
            return self._mint(connection, client.client_id or "", row["id"], code.scopes)

    def _load_token(self, secret: str, kind: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT t.* FROM remote_oauth_tokens t JOIN remote_oauth_clients c ON c.client_id=t.client_id "
                "WHERE t.token_hash=? AND t.kind=? AND t.revoked_at IS NULL AND c.revoked_at IS NULL AND c.approved_at IS NOT NULL",
                (secret_hash(secret), kind),
            ).fetchone()
        if row is None or int(row["expires_at"]) <= time.time() or row["resource_url"] != self.resource:
            return None
        return dict(row)

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = await self._thread(self._load_token, token, "access")
        return AccessToken(token=token, client_id=row["client_id"], scopes=json.loads(row["scopes_json"]),
            expires_at=int(row["expires_at"]), resource=self.resource, subject=row["client_id"],
            claims={"iss": self.issuer}) if row else None

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        row = await self._thread(self._load_token, refresh_token, "refresh")
        if row is None or row["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=row["client_id"], scopes=json.loads(row["scopes_json"]),
                            expires_at=int(row["expires_at"]), subject=row["client_id"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        return await self._thread(self._exchange_refresh, client, refresh_token, scopes)

    def _exchange_refresh(self, client: OAuthClientInformationFull, token: RefreshToken, scopes: list[str]) -> OAuthToken:
        with self.database.connect(write=True) as connection:
            row = self._load_token(token.token, "refresh")
            if row is None or row["client_id"] != client.client_id or not set(scopes).issubset(json.loads(row["scopes_json"])):
                raise TokenError("invalid_grant", "Refresh token is expired, revoked or already rotated")
            changed = connection.execute("UPDATE remote_oauth_tokens SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL", (iso_now(), row["token_hash"]))
            if changed.rowcount != 1:
                raise TokenError("invalid_grant", "Refresh token is already rotated")
            connection.execute("UPDATE remote_oauth_tokens SET revoked_at=? WHERE grant_id=? AND revoked_at IS NULL", (iso_now(), row["grant_id"]))
            self._audit(connection, client.client_id or "", "tokens.rotated", request_id=row["grant_id"])
            return self._mint(connection, client.client_id or "", row["grant_id"], scopes)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        await self._thread(self._revoke_token, token)

    def _revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with self.database.connect(write=True) as connection:
            row = connection.execute("SELECT grant_id,client_id FROM remote_oauth_tokens WHERE token_hash=?", (secret_hash(token.token),)).fetchone()
            if row is not None:
                connection.execute("UPDATE remote_oauth_tokens SET revoked_at=? WHERE grant_id=? AND revoked_at IS NULL", (iso_now(), row["grant_id"]))
                self._audit(connection, row["client_id"], "tokens.revoked", request_id=row["grant_id"])

    def revoke_client(self, client_id: str) -> dict[str, Any]:
        with self.database.connect(write=True) as connection:
            row = connection.execute("SELECT * FROM remote_oauth_clients WHERE client_id=?", (client_id,)).fetchone()
            if row is None:
                raise DomainError("oauth_client_unknown", "Client not found", status_code=404)
            now = iso_now()
            connection.execute("UPDATE remote_oauth_clients SET revoked_at=COALESCE(revoked_at,?) WHERE client_id=?", (now, client_id))
            connection.execute("UPDATE remote_oauth_tokens SET revoked_at=? WHERE client_id=? AND revoked_at IS NULL", (now, client_id))
            self._audit(connection, client_id, "client.revoked")
        return {"client_id": client_id, "revoked": True}
