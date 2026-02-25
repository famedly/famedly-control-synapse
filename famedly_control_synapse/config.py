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
from pydantic import BaseModel, Field, HttpUrl, field_validator


class FamedlyControlConfig(BaseModel):
    title: str = "Famedly Control module by Famedly"
    description: str = "Famedly Control module by Famedly"
    contact: str = "info@famedly.com"
    # TODO: use configured url and access token
    url: HttpUrl = Field(
        ..., description="HTTP or HTTPS URL for the Famedly Control API"
    )
    access_token: str = Field(
        ..., min_length=1, description="Access token for authentication"
    )
    api_key: str = Field(
        ...,
        min_length=1,
        description="API key for authenticating with Famedly Control API",
    )

    @field_validator("access_token", "api_key")
    @classmethod
    def validate_access_token(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("access_token cannot be empty or whitespace-only")
        return v


# TODO: configure the logging options
