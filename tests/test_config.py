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
            "url": "https://api.example.com",
            "access_token": "test_token_123",
            "api_key": "test_api_key_456",
            "auth_provider": "https://idp.example.com/",
        }
        config = FamedlyControl.parse_config(config_dict)

        assert config.url == HttpUrl("https://api.example.com")
        assert config.access_token == "test_token_123"
        assert config.api_key == "test_api_key_456"
        assert config.auth_provider == "https://idp.example.com/"

    def test_missing_url(self):
        """Test that missing URL raises ValidationError."""
        config_dict = {
            "access_token": "test_token",
        }

        with pytest.raises(ValidationError):
            FamedlyControl.parse_config(config_dict)

    def test_missing_access_token(self):
        """Test that missing access_token raises ValidationError."""
        config_dict = {
            "url": "https://api.example.com",
        }

        with pytest.raises(ValidationError):
            FamedlyControl.parse_config(config_dict)

    def test_invalid_url_scheme(self):
        """Test that non-HTTP/HTTPS URL raises ValidationError."""
        config_dict = {
            "url": "ftp://example.com",
            "access_token": "test_token",
        }

        with pytest.raises(ValidationError):
            FamedlyControl.parse_config(config_dict)

    def test_empty_access_token(self):
        """Test that empty access_token raises ValidationError."""
        config_dict = {
            "url": "https://api.example.com",
            "access_token": "",
        }

        with pytest.raises(ValidationError):
            FamedlyControl.parse_config(config_dict)

    def test_whitespace_only_access_token(self):
        """Test that whitespace-only access_token raises ValidationError."""
        config_dict = {
            "url": "https://api.example.com",
            "access_token": "   ",
        }

        with pytest.raises(ValidationError):
            FamedlyControl.parse_config(config_dict)
