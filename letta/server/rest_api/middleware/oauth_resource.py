"""Middleware that validates a Bearer JWT and provisions the Letta user.

When ``LETTA_OAUTH_ORG_REASSIGN`` is enabled the middleware will also
transparently re-assign the user's org if the OIDC claims now point to a
different organisation (see :mod:`letta.server.rest_api.oauth_user`).

The decoded claims dict is stored on ``request.state.oidc_claims`` and the
resolved :class:`~letta.schemas.user.User` on ``request.state.actor`` so that
downstream route handlers can access them without re-parsing the token.

Paths listed in ``BYPASS_PATHS`` are exempt from JWT validation.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Dict, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from letta.server.rest_api.oauth_user import OrgResolverConfig, get_or_provision_user_for_claims

logger = logging.getLogger(__name__)

BYPASS_PATHS = frozenset(
    {
        "/v1/health",
        "/v1/health/",
        "/v1/ready",
        "/v1/ready/",
        "/latest/health/",
        "/latest/ready",
        "/latest/ready/",
    }
)


def _decode_jwt_claims_unverified(token: str) -> Dict[str, Any]:
    """Return the claims payload from a JWT *without* signature verification.

    This is intentionally lightweight — the token has already been verified by
    an upstream ingress / API gateway before reaching Letta.  If you need
    in-process verification, swap this implementation for a call to a JWT
    library (e.g. ``python-jose`` or ``authlib``).
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Not a valid JWT (expected 3 dot-separated parts)")

    # JWT base64url uses ``-`` and ``_`` instead of ``+`` and ``/``, and
    # omits padding.  Add padding back before decoding.
    payload_b64 = parts[1]
    padding = 4 - len(payload_b64) % 4
    if padding != 4:
        payload_b64 += "=" * padding

    payload_bytes = base64.urlsafe_b64decode(payload_b64)
    return json.loads(payload_bytes)


class OAuthResourceMiddleware(BaseHTTPMiddleware):
    """Validate the ``Authorization: Bearer <jwt>`` header and provision the user."""

    def __init__(self, app, config: Optional[OrgResolverConfig] = None, default_org_id: Optional[str] = None):
        super().__init__(app)
        self._config = config or OrgResolverConfig.from_env()
        self._default_org_id = default_org_id

    async def dispatch(self, request: Request, call_next):
        if request.url.path in BYPASS_PATHS:
            return await call_next(request)

        auth_header: Optional[str] = request.headers.get("Authorization")
        if not auth_header or not auth_header.lower().startswith("bearer "):
            return JSONResponse({"detail": "Missing or malformed Authorization header"}, status_code=401)

        token = auth_header[len("Bearer "):].strip()

        try:
            claims = _decode_jwt_claims_unverified(token)
        except Exception as exc:
            logger.debug("JWT decode failed: %s", exc)
            return JSONResponse({"detail": "Invalid token"}, status_code=401)

        if "iss" not in claims or "sub" not in claims:
            return JSONResponse({"detail": "Token missing required claims (iss, sub)"}, status_code=401)

        try:
            actor = await get_or_provision_user_for_claims(
                claims=claims,
                config=self._config,
                default_org_id=self._default_org_id,
            )
        except Exception as exc:
            logger.exception("User provisioning failed: %s", exc)
            return JSONResponse({"detail": "User provisioning failed"}, status_code=500)

        request.state.oidc_claims = claims
        request.state.actor = actor

        return await call_next(request)
