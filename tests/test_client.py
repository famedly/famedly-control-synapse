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
from unittest.mock import AsyncMock, MagicMock, patch

from parameterized import parameterized
from synapse.api.errors import HttpResponseException
from synapse.server import HomeServer
from synapse.util.clock import Clock
from twisted.internet import defer
from twisted.internet.defer import ensureDeferred
from twisted.internet.testing import MemoryReactor
from twisted.trial import unittest
from twisted.web.http_headers import Headers

from famedly_control_synapse.client import FamedlyControlClient, FamedlyControlError
from famedly_control_synapse.config import FamedlyControlConfig
from tests.utils.module_api_testcase import ModuleApiTestCase


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


class TestClientResponse(ModuleApiTestCase):

    def prepare(self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer):
        super().prepare(reactor, clock, homeserver)
        self.client = self.hs.room_control.client

    def test_auth_header_is_single_bearer_token(self) -> None:
        """Regression: Authorization header must arrive as one value, not one per character.

        Synapse's SimpleHttpClient.post_json_get_json expects header values to be
        lists. Passing a bare string causes Twisted to iterate over each character
        and emit a separate Authorization header per character.
        """
        captured: dict[str, Headers] = {}
        mock_response = MagicMock()
        mock_response.code = 200

        async def fake_request(method, uri, headers, data=None):
            captured["headers"] = headers
            return mock_response

        self.client.http_client.request = fake_request

        with patch(
            "synapse.http.client.readBody",
            return_value=defer.succeed(b'{"Ok": {"members": []}}'),
        ):
            self.get_success(self.client.get_group_members("test_group"))

        assert isinstance(captured.get("headers"), Headers)
        auth_values = captured["headers"].getRawHeaders(b"authorization")
        assert auth_values == [b"Bearer dummy_token_for_testing"]

    def test_request_success(self) -> None:
        """Test that client returns the expected list of member IDs on success."""
        expected_members = ["user1_external_id", "user2_external_id"]

        self.client.http_client.post_json_get_json = AsyncMock(
            return_value={
                "Ok": {
                    "members": [
                        {"user_id": "user1_external_id"},
                        {"user_id": "user2_external_id"},
                    ]
                }
            }
        )
        members = self.get_success(self.client.get_group_members("test_group"))
        assert members == expected_members

    @parameterized.expand([("Forbidden", 403), ("Unauthorized", 401)])
    def test_request_fail_with_err_response_types(
        self, error_type, expected_code
    ) -> None:
        """Test that client returns 200 with Err message raises FamedlyControlError with proper code."""
        self.client.http_client.post_json_get_json = AsyncMock(
            return_value={"Err": {"type": error_type}}
        )
        failure = self.get_failure(
            self.client.get_group_members("test_group"), FamedlyControlError
        )
        assert failure.value.code == expected_code
        assert (
            failure.value.msg == f"Famedly Control API: Error in response: {error_type}"
        )

    def test_request_fail_with_err_response_unknown_type(self) -> None:
        """Test that client returns 200 with Err message with unknown type raises FamedlyControlError with 500 code."""
        self.client.http_client.post_json_get_json = AsyncMock(
            return_value={"Err": {"type": "SomeUnknownType"}}
        )
        failure = self.get_failure(
            self.client.get_group_members("test_group"), FamedlyControlError
        )
        self.assertEqual(failure.value.code, 500)
        assert failure.value.code == 500
        assert (
            failure.value.msg
            == "Famedly Control API: Error in response: SomeUnknownType"
        )

    def test_request_fail_with_err_response_not_dict(self) -> None:
        """Test that client returns 200 with Err message with non dict format raises FamedlyControlError with 500 code."""
        self.client.http_client.post_json_get_json = AsyncMock(
            return_value={"Err": "SomeError"}
        )
        failure = self.get_failure(
            self.client.get_group_members("test_group"), FamedlyControlError
        )
        assert failure.value.code == 500
        assert (
            failure.value.msg
            == "Famedly Control API: Unexpected error: 'str' object has no attribute 'get'"
        )

    def test_request_fail_with_unexpected_response(self) -> None:
        """Test that client raises FamedlyControlError when response format is neither "Ok" nor "Err"."""
        self.client.http_client.post_json_get_json = AsyncMock(
            return_value={"Unexpected": "format"}
        )
        failure = self.get_failure(
            self.client.get_group_members("test_group"), FamedlyControlError
        )
        assert failure.value.code == 502
        assert (
            failure.value.msg
            == "Famedly Control API: Unexpected response format: {'Unexpected': 'format'}"
        )

    def test_request_fail_with_http_exception(self) -> None:
        """Test that client raises any other HTTP error raises FamedlyControlError."""
        self.client.http_client.post_json_get_json = AsyncMock(
            side_effect=HttpResponseException(500, "Internal Server Error", b"")
        )
        failure = self.get_failure(
            self.client.get_group_members("test_group"), FamedlyControlError
        )
        assert failure.value.code == 500
        assert (
            failure.value.msg
            == "Famedly Control API: HTTP response error: Internal Server Error"
        )

    def test_request_fail_with_validation_error(self) -> None:
        """Test that client raises FamedlyControlError on validation error."""
        self.client.http_client.post_json_get_json = AsyncMock(
            return_value={
                "Ok": {"members": "not_a_list"}
            }  # invalid: members must be a list
        )
        failure = self.get_failure(
            self.client.get_group_members("test_group"), FamedlyControlError
        )
        assert failure.value.code == 500
        assert failure.value.msg.startswith(
            "Famedly Control API: Unexpected error: 1 validation error for GroupMembersResponse"
        )

    def test_request_fail_with_generic_exception(self) -> None:
        """Test that client raises FamedlyControlError on any unexpected exception."""
        self.client.http_client.post_json_get_json = AsyncMock(
            side_effect=RuntimeError("something broke")
        )
        failure = self.get_failure(
            self.client.get_group_members("test_group"), FamedlyControlError
        )
        assert failure.value.code == 500
        assert (
            failure.value.msg
            == "Famedly Control API: Unexpected error: something broke"
        )
