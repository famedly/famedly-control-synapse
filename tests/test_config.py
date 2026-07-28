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
import pytest
from pydantic import HttpUrl, ValidationError

from famedly_control_synapse.famedly_control import FamedlyControl
from tests.utils.jwt_keys import JWK_PATH, SERVICE_ACCOUNT_PATH


def _jwt_auth(**overrides):
    auth = {
        "token_endpoint": "https://idp.example.com/oauth/v2/token",
        "aud": "https://idp.example.com",
        "iss": "service-user",
        "sub": "service-user",
        "jwk_path": JWK_PATH,
    }
    auth.update(overrides)
    return auth


class TestConfigParsing:
    def test_valid_config(self):
        """Test parsing a valid config with all required fields."""
        config_dict = {
            "famedly_control": {
                "api_url": "https://api.example.com",
                "jwt_auth": _jwt_auth(),
            },
            "auth_provider": "https://idp.example.com/",
            "admin_user": "admin",
        }
        config = FamedlyControl.parse_config(config_dict)

        assert config.famedly_control.api_url == HttpUrl("https://api.example.com")
        assert config.famedly_control.jwt_auth.jwk_path == JWK_PATH
        assert config.auth_provider == "https://idp.example.com/"
        assert config.admin_user == "admin"

    def test_missing_famedly_control(self):
        """Test that missing famedly_control raises ValidationError."""
        config_dict = {
            "auth_provider": "https://idp.example.com/",
            "admin_user": "admin",
        }

        with pytest.raises(ValidationError):
            FamedlyControl.parse_config(config_dict)

    def test_missing_api_url(self):
        """Test that missing api_url raises ValidationError."""
        config_dict = {
            "famedly_control": {
                "jwt_auth": _jwt_auth(),
            },
            "auth_provider": "https://idp.example.com/",
            "admin_user": "admin",
        }

        with pytest.raises(ValidationError):
            FamedlyControl.parse_config(config_dict)

    def test_missing_jwt_auth(self):
        """Test that missing jwt_auth raises ValidationError."""
        config_dict = {
            "famedly_control": {
                "api_url": "https://api.example.com",
            },
            "auth_provider": "https://idp.example.com/",
            "admin_user": "admin",
        }

        with pytest.raises(ValidationError):
            FamedlyControl.parse_config(config_dict)

    def test_invalid_url_scheme(self):
        """Test that non-HTTP/HTTPS URL raises ValidationError."""
        config_dict = {
            "famedly_control": {
                "api_url": "ftp://example.com",
                "jwt_auth": _jwt_auth(),
            },
            "auth_provider": "https://idp.example.com/",
            "admin_user": "admin",
        }

        with pytest.raises(ValidationError):
            FamedlyControl.parse_config(config_dict)

    def test_no_key_source(self):
        """Neither jwk_path nor zitadel_service_account_path set is rejected."""
        auth = _jwt_auth()
        del auth["jwk_path"]
        config_dict = {
            "famedly_control": {"api_url": "https://api.example.com", "jwt_auth": auth},
            "auth_provider": "https://idp.example.com/",
        }

        with pytest.raises(ValidationError):
            FamedlyControl.parse_config(config_dict)

    def test_both_key_sources(self):
        """Setting both key sources at once is rejected."""
        config_dict = {
            "famedly_control": {
                "api_url": "https://api.example.com",
                "jwt_auth": _jwt_auth(
                    zitadel_service_account_path=SERVICE_ACCOUNT_PATH
                ),
            },
            "auth_provider": "https://idp.example.com/",
            "admin_user": "admin",
        }

        with pytest.raises(ValidationError):
            FamedlyControl.parse_config(config_dict)

    def test_jwk_requires_iss_sub(self):
        """iss and sub are required when using a raw JWK."""
        auth = _jwt_auth()
        del auth["iss"]
        config_dict = {
            "famedly_control": {"api_url": "https://api.example.com", "jwt_auth": auth},
            "auth_provider": "https://idp.example.com/",
        }

        with pytest.raises(ValidationError):
            FamedlyControl.parse_config(config_dict)

    def test_service_account_without_iss_sub(self):
        """A service account file may omit iss/sub (derived from userId)."""
        config_dict = {
            "famedly_control": {
                "api_url": "https://api.example.com",
                "jwt_auth": {
                    "token_endpoint": "https://idp.example.com/oauth/v2/token",
                    "aud": "https://idp.example.com",
                    "zitadel_service_account_path": SERVICE_ACCOUNT_PATH,
                },
            },
            "auth_provider": "https://idp.example.com/",
            "admin_user": "admin",
        }
        config = FamedlyControl.parse_config(config_dict)
        assert config.famedly_control.jwt_auth.iss is None
        assert (
            config.famedly_control.jwt_auth.zitadel_service_account_path
            == SERVICE_ACCOUNT_PATH
        )

    def test_missing_admin_user(self):
        """Test that missing admin_user raises ValidationError."""
        config_dict = {
            "famedly_control": {
                "api_url": "https://api.example.com",
                "jwt_auth": _jwt_auth(),
            },
            "auth_provider": "https://idp.example.com/",
        }

        with pytest.raises(ValidationError):
            FamedlyControl.parse_config(config_dict)

    def test_empty_admin_user(self):
        """Test that empty admin_user raises ValidationError."""
        config_dict = {
            "famedly_control": {
                "api_url": "https://api.example.com",
                "jwt_auth": _jwt_auth(),
            },
            "auth_provider": "https://idp.example.com/",
            "admin_user": "",
        }

        with pytest.raises(ValidationError):
            FamedlyControl.parse_config(config_dict)

    @pytest.mark.parametrize(
        "admin_user",
        ["@admin:example.com", "@admin", "admin:example.com", "   "],
    )
    def test_invalid_admin_user(self, admin_user):
        """Test that a full MXID or whitespace-only admin_user raises
        ValidationError; only a localpart is accepted."""
        config_dict = {
            "famedly_control": {
                "api_url": "https://api.example.com",
                "jwt_auth": _jwt_auth(),
            },
            "auth_provider": "https://idp.example.com/",
            "admin_user": admin_user,
        }
        with pytest.raises(ValidationError):
            FamedlyControl.parse_config(config_dict)
