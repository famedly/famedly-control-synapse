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
    auth_provider: str = Field(
        ...,
        description="The unique, internal ID of the external identity provider, used in the database to link external user IDs to Matrix user IDs",
    )

    @field_validator("auth_provider")
    @classmethod
    def validate_auth_provider(cls, v: str, info: ValidationInfo) -> str:
        return _validate_not_blank(v, info)
