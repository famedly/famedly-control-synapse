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
from unittest.mock import AsyncMock, MagicMock

from twisted.internet.defer import ensureDeferred
from twisted.trial import unittest

from famedly_control_synapse.client import FamedlyControlClient
from famedly_control_synapse.config import FamedlyControlConfig


def _make_client(api_url: str):
    config = FamedlyControlConfig(
        famedly_control={
            "api_url": api_url,
            "access_token": "test_token",
        },
        auth_provider="https://idp.example.com/",
    )
    api = MagicMock()
    api.http_client = MagicMock()
    api.http_client.post_json_get_json = AsyncMock()
    return api, FamedlyControlClient(api, config)


class TestClientUrlConstruction(unittest.TestCase):
    """Ensure the client builds endpoint URLs without double slashes."""

    api_urls_with_path = [
        "http://api.example.com/famedlyControl",
        "http://api.example.com/famedlyControl/",
    ]

    api_urls_bare_domain = [
        "http://api.example.com",
        "http://api.example.com/",
    ]

    def test_get_group_members_url_with_path(self):
        for api_url in self.api_urls_with_path:
            api, client = _make_client(api_url)
            api.http_client.post_json_get_json.return_value = {"Ok": {"members": []}}
            self.successResultOf(ensureDeferred(client.get_group_members("some_group")))

            called_uri = api.http_client.post_json_get_json.call_args[0][0]
            self.assertEqual(
                called_uri,
                "http://api.example.com/famedlyControl/get_group_members",
                f"Failed for api_url={api_url!r}",
            )

    def test_get_group_members_url_bare_domain(self):
        for api_url in self.api_urls_bare_domain:
            api, client = _make_client(api_url)
            api.http_client.post_json_get_json.return_value = {"Ok": {"members": []}}
            self.successResultOf(ensureDeferred(client.get_group_members("some_group")))

            called_uri = api.http_client.post_json_get_json.call_args[0][0]
            self.assertEqual(
                called_uri,
                "http://api.example.com/get_group_members",
                f"Failed for api_url={api_url!r}",
            )

    def test_get_all_groups_diffs_url_with_path(self):
        for api_url in self.api_urls_with_path:
            api, client = _make_client(api_url)
            api.http_client.post_json_get_json.return_value = {
                "Ok": {"next_sync": "1", "data": {}}
            }
            self.successResultOf(ensureDeferred(client.get_all_groups_diffs(sync=None)))

            called_uri = api.http_client.post_json_get_json.call_args[0][0]
            self.assertEqual(
                called_uri,
                "http://api.example.com/famedlyControl/get_all_groups_diffs",
                f"Failed for api_url={api_url!r}",
            )

    def test_get_all_groups_diffs_url_bare_domain(self):
        for api_url in self.api_urls_bare_domain:
            api, client = _make_client(api_url)
            api.http_client.post_json_get_json.return_value = {
                "Ok": {"next_sync": "1", "data": {}}
            }
            self.successResultOf(ensureDeferred(client.get_all_groups_diffs(sync=None)))

            called_uri = api.http_client.post_json_get_json.call_args[0][0]
            self.assertEqual(
                called_uri,
                "http://api.example.com/get_all_groups_diffs",
                f"Failed for api_url={api_url!r}",
            )
