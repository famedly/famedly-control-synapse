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


class TestConfigParsing:
    def test_valid_config(self):
        """Test parsing a valid config with all required fields."""
        config_dict = {
            "famedly_control": {
                "api_url": "https://api.example.com",
                "access_token": "test_token_123",
            },
            "auth_provider": "https://idp.example.com/",
        }
        config = FamedlyControl.parse_config(config_dict)

        assert config.famedly_control.api_url == HttpUrl("https://api.example.com")
        assert config.famedly_control.access_token == "test_token_123"
        assert config.auth_provider == "https://idp.example.com/"

    def test_missing_famedly_control(self):
        """Test that missing famedly_control raises ValidationError."""
        config_dict = {
            "auth_provider": "https://idp.example.com/",
        }

        with pytest.raises(ValidationError):
            FamedlyControl.parse_config(config_dict)

    def test_missing_api_url(self):
        """Test that missing api_url raises ValidationError."""
        config_dict = {
            "famedly_control": {
                "access_token": "test_token",
            },
            "auth_provider": "https://idp.example.com/",
        }

        with pytest.raises(ValidationError):
            FamedlyControl.parse_config(config_dict)

    def test_missing_access_token(self):
        """Test that missing access_token raises ValidationError."""
        config_dict = {
            "famedly_control": {
                "api_url": "https://api.example.com",
            },
            "auth_provider": "https://idp.example.com/",
        }

        with pytest.raises(ValidationError):
            FamedlyControl.parse_config(config_dict)

    def test_invalid_url_scheme(self):
        """Test that non-HTTP/HTTPS URL raises ValidationError."""
        config_dict = {
            "famedly_control": {
                "api_url": "ftp://example.com",
                "access_token": "test_token",
            },
            "auth_provider": "https://idp.example.com/",
        }

        with pytest.raises(ValidationError):
            FamedlyControl.parse_config(config_dict)

    def test_empty_access_token(self):
        """Test that empty access_token raises ValidationError."""
        config_dict = {
            "famedly_control": {
                "api_url": "https://api.example.com",
                "access_token": "",
            },
            "auth_provider": "https://idp.example.com/",
        }

        with pytest.raises(ValidationError):
            FamedlyControl.parse_config(config_dict)

    def test_whitespace_only_access_token(self):
        """Test that whitespace-only access_token raises ValidationError."""
        config_dict = {
            "famedly_control": {
                "api_url": "https://api.example.com",
                "access_token": "   ",
            },
            "auth_provider": "https://idp.example.com/",
        }

        with pytest.raises(ValidationError):
            FamedlyControl.parse_config(config_dict)
