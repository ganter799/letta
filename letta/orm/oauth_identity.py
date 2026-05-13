from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from letta.orm.sqlalchemy_base import SqlalchemyBase


class OAuthIdentity(SqlalchemyBase):
    """Maps an OIDC (iss, sub) pair to a Letta user.

    Created on first login; looked up on every subsequent login to retrieve the
    provisioned Letta user without having to re-inspect the raw JWT.
    """

    __tablename__ = "oauth_identities"
    __table_args__ = (
        UniqueConstraint("iss", "sub", name="uq_oauth_iss_sub"),
        Index("ix_oauth_identities_user_id", "user_id"),
    )

    iss: Mapped[str] = mapped_column(String, nullable=False, doc="OIDC issuer URL (e.g. https://accounts.google.com)")
    sub: Mapped[str] = mapped_column(String, nullable=False, doc="OIDC subject identifier (opaque user id from the IdP)")
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        doc="Letta user this OIDC identity maps to",
    )
