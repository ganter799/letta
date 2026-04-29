import os
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class ProtectedResourceMetadata(BaseModel):
    resource: str
    authorization_servers: List[str]
    bearer_methods_supported: List[str] = ["header"]
    scopes_supported: Optional[List[str]] = None
    resource_documentation: Optional[str] = None


@router.get(
    "/.well-known/oauth-protected-resource",
    tags=["auth"],
    include_in_schema=False,
    response_model=ProtectedResourceMetadata,
)
def oauth_protected_resource_metadata() -> ProtectedResourceMetadata:
    """RFC 9728 OAuth 2.0 Protected Resource Metadata.

    Returns 404-equivalent (empty issuer) if OAuth is not configured.
    """
    issuer = os.getenv("LETTA_OAUTH_ISSUER", "").rstrip("/")
    resource = os.getenv("LETTA_OAUTH_RESOURCE_URL") or os.getenv("LETTA_BASE_URL", "")
    scopes_env = os.getenv("LETTA_OAUTH_SCOPES_SUPPORTED", "openid email offline_access")
    scopes = [s for s in scopes_env.split() if s] or None

    return ProtectedResourceMetadata(
        resource=resource,
        authorization_servers=[issuer] if issuer else [],
        bearer_methods_supported=["header"],
        scopes_supported=scopes,
    )
