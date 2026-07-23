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
from pydantic import BaseModel, Field, HttpUrl, ValidationInfo, field_validator


def _validate_not_blank(v: str, info: ValidationInfo) -> str:
    if not v.strip():
        raise ValueError(f"{info.field_name} cannot be empty or whitespace-only")
    return v


class FamedlyControlApiConfig(BaseModel):
    api_url: HttpUrl = Field(
        ..., description="HTTP or HTTPS URL for the Famedly Control API"
    )
    access_token: str = Field(
        ...,
        min_length=1,
        description="Access token to authenticate against Famedly Control",
    )

    @field_validator("access_token")
    @classmethod
    def validate_token_fields(cls, v: str, info: ValidationInfo) -> str:
        return _validate_not_blank(v, info)


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
            raise ValueError(
                "admin_user must only contain the localpart (e.g. 'admin'), not a full "
                "Matrix user ID"
            )
        return v
