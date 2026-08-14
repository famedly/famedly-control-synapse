# Copyright (C) 2026 Famedly
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
from pydantic import (
    BaseModel,
    Field,
    HttpUrl,
    ValidationInfo,
    field_validator,
    model_validator,
)


class ConfigValidationError(ValueError):
    """Raised when the configuration is invalid or incomplete."""


def _validate_not_blank(v: str, info: ValidationInfo) -> str:
    if not v.strip():
        msg = f"{info.field_name} cannot be empty or whitespace-only"
        raise ConfigValidationError(msg)
    return v


class JwtAuthConfig(BaseModel):
    """OAuth2 private-key-JWT authentication against an OIDC IdP (e.g. Zitadel).

    The module signs a short-lived JWT assertion
    with a private key and exchanges it at ``token_endpoint`` for an access token,
    which is then used as the Bearer credential for Famedly Control requests. The
    access token is refreshed automatically before it expires.
    """

    token_endpoint: HttpUrl = Field(
        ...,
        description="Token exchange endpoint, e.g. https://${CUSTOM_DOMAIN}/oauth/v2/token",
    )
    aud: str = Field(
        ...,
        min_length=1,
        description="Audience of the JWT assertion; the domain of the Zitadel instance",
    )
    iss: str | None = Field(
        default=None,
        description="Issuer of the JWT assertion; the service user id. Required unless "
        "derivable from the Zitadel service account file.",
    )
    sub: str | None = Field(
        default=None,
        description="Subject of the JWT assertion; the service user id. Required unless "
        "derivable from the Zitadel service account file.",
    )
    scopes: list[str] = Field(
        default_factory=list,
        description="Scopes requested during the token exchange. 'openid' is always "
        "added, so this is optional if that is the only scope needed.",
    )
    token_lifetime: int = Field(
        default=3600,
        ge=1,
        description="Lifetime in seconds of the signed JWT assertion; used to compute "
        "the 'exp' claim from 'iat'.",
    )
    jwk_path: str | None = Field(
        default=None,
        description="Path to a JSON Web Key holding the private key, with 'kid' and 'alg' "
        "set. Required unless zitadel_service_account_path is set.",
    )
    zitadel_service_account_path: str | None = Field(
        default=None,
        description="Path to a Zitadel service account JSON (RSA private key + keyId + "
        "userId). Converted internally to a JWK. Alternative to jwk_path.",
    )

    @field_validator("aud")
    @classmethod
    def validate_aud(cls, v: str, info: ValidationInfo) -> str:
        return _validate_not_blank(v, info)

    @field_validator("iss", "sub")
    @classmethod
    def validate_iss_sub(cls, v: str | None, info: ValidationInfo) -> str | None:
        # Optional, but if given they must not be whitespace-only.
        return v if v is None else _validate_not_blank(v, info)

    @model_validator(mode="after")
    def validate_key_source(self) -> "JwtAuthConfig":
        if bool(self.jwk_path) == bool(self.zitadel_service_account_path):
            msg = "exactly one of jwk_path or zitadel_service_account_path must be set"
            raise ConfigValidationError(msg)
        # iss/sub can be derived from the service account file's userId; when using a
        # raw JWK there is no such fallback, so they must be provided explicitly.
        if self.jwk_path and (not self.iss or not self.sub):
            msg = "iss and sub are required when using jwk_path"
            raise ConfigValidationError(msg)
        return self


class FamedlyControlApiConfig(BaseModel):
    api_url: HttpUrl = Field(
        ..., description="HTTP or HTTPS URL for the Famedly Control API"
    )
    jwt_auth: JwtAuthConfig = Field(
        ...,
        description="OAuth2 private-key-JWT authentication against the IdP",
    )


class FamedlyControlConfig(BaseModel):
    famedly_control: FamedlyControlApiConfig
    sync_enabled: bool = Field(
        default=True,
        description="Whether to run the background group membership sync loop",
    )
    sync_polling_interval_seconds: int = Field(
        default=30,
        ge=1,
        description="Delay in seconds before retrying after a sync failure",
    )
    error_retry_queue_enabled: bool = Field(
        default=True,
        description="Enable retrying membership changes that previously errored",
    )
    error_retry_queue_interval_seconds: int = Field(
        default=30,
        ge=1,
        description="Delay in seconds before attempting a membership adjustment from the error retry queue",
    )
    error_retry_queue_log_after_retry_count: int = Field(
        default=3,
        ge=1,
        description="The count of error retry queue attempts to start logging warnings",
    )
    auth_provider: str = Field(
        ...,
        description="The unique, internal ID of the external identity provider, used in the database to link external user IDs to Matrix user IDs",
    )
    admin_user: str = Field(
        ...,
        description="The localpart of the matrix user ID of the sole administrator permitted to call the Famedly Control API.",
    )

    @field_validator("auth_provider")
    @classmethod
    def validate_auth_provider(cls, v: str, info: ValidationInfo) -> str:
        return _validate_not_blank(v, info)

    @field_validator("admin_user")
    @classmethod
    def validate_admin_user(cls, v: str, info: ValidationInfo) -> str:
        v = _validate_not_blank(v, info)
        if "@" in v or ":" in v:
            msg = "admin_user must only contain the localpart (e.g. 'admin'), not a full Matrix user ID"
            raise ConfigValidationError(msg)
        return v
