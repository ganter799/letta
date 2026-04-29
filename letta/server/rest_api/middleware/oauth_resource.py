import asyncio
import os
import time
from typing import Iterable, Optional, Sequence
from urllib.parse import urljoin, urlparse

import httpx
from authlib.jose import JsonWebKey, JsonWebToken
from authlib.jose.errors import BadSignatureError, JoseError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from letta.log import get_logger

logger = get_logger(__name__)

_DEFAULT_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "PS256")
_JWKS_TTL_SECONDS = 3600
_JWKS_FETCH_TIMEOUT = 5.0

_OPEN_PATHS = frozenset(
    {
        "/v1/health",
        "/v1/health/",
        "/latest/health",
        "/latest/health/",
        "/v1/ready",
        "/v1/ready/",
        "/latest/ready",
        "/latest/ready/",
        "/.well-known/oauth-protected-resource",
        "/.well-known/openid-configuration",
    }
)


class OAuthResourceMiddleware(BaseHTTPMiddleware):
    """Validate Authentik-issued bearer JWTs as a protected resource (RFC 6750/9728)."""

    def __init__(
        self,
        app,
        issuer: str,
        audience: Optional[Sequence[str]] = None,
        jwks_uri: Optional[str] = None,
        algorithms: Sequence[str] = _DEFAULT_ALGORITHMS,
        required_scopes: Optional[Iterable[str]] = None,
        leeway: int = 30,
        skip_audience: bool = False,
        provision_users: bool = False,
    ):
        super().__init__(app)
        self._issuer = issuer.rstrip("/") + "/"
        self._audience = list(audience) if audience else []
        self._configured_jwks_uri = jwks_uri
        self._algorithms = list(algorithms)
        self._required_scopes = set(required_scopes or ())
        self._leeway = leeway
        self._skip_audience = skip_audience
        self._provision_users = provision_users

        self._jwt = JsonWebToken(self._algorithms)
        self._keyset = None
        self._keyset_fetched_at: float = 0.0
        self._keyset_lock = asyncio.Lock()

    async def dispatch(self, request, call_next):
        if request.url.path in _OPEN_PATHS:
            return await call_next(request)

        token = self._extract_bearer(request.headers.get("Authorization"))
        if not token:
            return self._unauthorized("invalid_request", "Missing bearer token")

        try:
            keyset = await self._get_keyset()
        except Exception as exc:
            logger.error("OAuth: failed to fetch JWKS: %s", exc)
            return self._unauthorized(
                "temporarily_unavailable",
                "Auth server JWKS could not be retrieved",
                status_code=503,
            )

        try:
            claims = self._jwt.decode(token, key=keyset, claims_options=self._claims_options())
            claims.validate(leeway=self._leeway)
        except BadSignatureError:
            # Signing key may have rotated; refresh JWKS once and retry.
            try:
                keyset = await self._get_keyset(force=True)
                claims = self._jwt.decode(token, key=keyset, claims_options=self._claims_options())
                claims.validate(leeway=self._leeway)
            except JoseError as retry_exc:
                logger.info("OAuth: token rejected after JWKS refresh: %s", retry_exc)
                return self._unauthorized("invalid_token", str(retry_exc))
        except JoseError as exc:
            logger.info("OAuth: token rejected: %s", exc)
            return self._unauthorized("invalid_token", str(exc))
        except Exception as exc:
            logger.exception("OAuth: unexpected token validation error")
            return self._unauthorized("invalid_token", str(exc))

        if self._required_scopes and not self._has_required_scopes(claims):
            return self._unauthorized(
                "insufficient_scope",
                f"Required scopes: {' '.join(sorted(self._required_scopes))}",
                status_code=403,
            )

        request.state.oauth_claims = dict(claims)
        request.state.oauth_subject = claims.get("sub")
        request.state.oauth_token = token

        if self._provision_users and request.state.oauth_subject:
            from letta.server.rest_api.oauth_user import (
                OrgResolverConfig,
                get_or_provision_user_for_claims,
            )

            try:
                user_id = await get_or_provision_user_for_claims(
                    request.state.oauth_claims,
                    org_resolver=OrgResolverConfig.from_env(),
                )
            except Exception:
                logger.exception("OAuth: user provisioning failed")
                return self._unauthorized(
                    "server_error",
                    "User provisioning failed",
                    status_code=500,
                )
            if user_id:
                request.state.letta_user_id = user_id
                _set_scope_header(request.scope, b"user_id", user_id.encode("ascii"))

        return await call_next(request)

    @staticmethod
    def _extract_bearer(header_value: Optional[str]) -> Optional[str]:
        if not header_value:
            return None
        parts = header_value.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return None
        return parts[1].strip() or None

    def _claims_options(self) -> dict:
        issuer_no_slash = self._issuer.rstrip("/")
        options: dict = {
            "iss": {"essential": True, "values": [issuer_no_slash, issuer_no_slash + "/"]},
            "exp": {"essential": True},
        }
        if self._skip_audience or not self._audience:
            return options
        if len(self._audience) == 1:
            options["aud"] = {"essential": True, "value": self._audience[0]}
        else:
            options["aud"] = {"essential": True, "values": self._audience}
        return options

    def _has_required_scopes(self, claims) -> bool:
        scope_claim = claims.get("scope") or claims.get("scp") or ""
        if isinstance(scope_claim, list):
            granted = set(scope_claim)
        else:
            granted = set(str(scope_claim).split())
        return self._required_scopes.issubset(granted)

    async def _get_keyset(self, force: bool = False):
        now = time.monotonic()
        if (
            not force
            and self._keyset is not None
            and (now - self._keyset_fetched_at) < _JWKS_TTL_SECONDS
        ):
            return self._keyset

        async with self._keyset_lock:
            now = time.monotonic()
            if (
                not force
                and self._keyset is not None
                and (now - self._keyset_fetched_at) < _JWKS_TTL_SECONDS
            ):
                return self._keyset

            jwks_uri = self._configured_jwks_uri or await self._discover_jwks_uri()
            async with httpx.AsyncClient(timeout=_JWKS_FETCH_TIMEOUT) as client:
                resp = await client.get(jwks_uri)
                resp.raise_for_status()
                jwks = resp.json()
            self._keyset = JsonWebKey.import_key_set(jwks)
            self._keyset_fetched_at = time.monotonic()
            return self._keyset

    async def _discover_jwks_uri(self) -> str:
        discovery_url = urljoin(self._issuer, ".well-known/openid-configuration")
        async with httpx.AsyncClient(timeout=_JWKS_FETCH_TIMEOUT) as client:
            resp = await client.get(discovery_url)
            resp.raise_for_status()
            data = resp.json()
        jwks_uri = data.get("jwks_uri")
        if not jwks_uri:
            raise RuntimeError(f"No jwks_uri in discovery document at {discovery_url}")
        return jwks_uri

    def _unauthorized(
        self,
        error: str,
        description: str,
        status_code: int = 401,
    ) -> JSONResponse:
        realm = urlparse(self._issuer).netloc or "letta"
        challenge = (
            f'Bearer realm="{realm}", error="{error}", '
            f'error_description="{description}"'
        )
        headers = {"WWW-Authenticate": challenge}
        return JSONResponse(
            status_code=status_code,
            content={"detail": description, "error": error},
            headers=headers,
        )


def _set_scope_header(scope: dict, name: bytes, value: bytes) -> None:
    """Replace any existing header `name` (case-insensitive) and append the new pair."""
    name_lower = name.lower()
    headers = [(k, v) for (k, v) in scope.get("headers", []) if k.lower() != name_lower]
    headers.append((name, value))
    scope["headers"] = headers


def build_oauth_middleware_kwargs() -> Optional[dict]:
    """Read env config for the OAuth middleware. Returns None if OAuth is not configured."""
    issuer = os.getenv("LETTA_OAUTH_ISSUER")
    if not issuer:
        return None

    audience_env = os.getenv("LETTA_OAUTH_AUDIENCE", "")
    audience = [a.strip() for a in audience_env.split(",") if a.strip()] or None

    scopes_env = os.getenv("LETTA_OAUTH_REQUIRED_SCOPES", "")
    required_scopes = [s.strip() for s in scopes_env.split(",") if s.strip()] or None

    skip_audience = os.getenv("LETTA_OAUTH_SKIP_AUD", "").lower() in {"1", "true", "yes"}
    provision_users = os.getenv("LETTA_OAUTH_PROVISION_USERS", "").lower() in {"1", "true", "yes"}

    return {
        "issuer": issuer,
        "audience": audience,
        "jwks_uri": os.getenv("LETTA_OAUTH_JWKS_URI") or None,
        "required_scopes": required_scopes,
        "skip_audience": skip_audience,
        "provision_users": provision_users,
    }
