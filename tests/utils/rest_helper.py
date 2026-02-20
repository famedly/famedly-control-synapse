# Copyright 2014-2016 OpenMarket Ltd
# Copyright 2017 Vector Creations Ltd
# Copyright 2018-2019 New Vector Ltd
# Copyright 2019-2021 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import time
from collections.abc import Iterable
from http import HTTPStatus
from typing import Any, AnyStr, Literal, overload
from urllib.parse import urlencode

import attr
from synapse.api.constants import Membership
from synapse.api.errors import Codes
from synapse.server import HomeServer
from synapse.types import JsonDict
from twisted.internet.testing import MemoryReactorClock
from twisted.web.server import Site

from tests.utils.server import make_request

# an 'oidc_config' suitable for login_via_oidc.
TEST_OIDC_ISSUER = "https://issuer.test/"
TEST_OIDC_CONFIG = {
    "enabled": True,
    "issuer": TEST_OIDC_ISSUER,
    "client_id": "test-client-id",
    "client_secret": "test-client-secret",
    "scopes": ["openid"],
    "user_mapping_provider": {"config": {"localpart_template": "{{ user.sub }}"}},
}


@attr.s(auto_attribs=True)
class RestHelper:
    """Contains extra helper functions to quickly and clearly perform a given
    REST action, which isn't the focus of the test.
    """

    hs: HomeServer
    reactor: MemoryReactorClock
    site: Site
    auth_user_id: str | None

    @overload
    def create_room_as(
        self,
        room_creator: str | None = ...,
        is_public: bool | None = ...,
        room_version: str | None = ...,
        tok: str | None = ...,
        expect_code: Literal[200] = ...,
        extra_content: dict | None = ...,
        custom_headers: Iterable[tuple[AnyStr, AnyStr]] | None = ...,
    ) -> str: ...

    @overload
    def create_room_as(
        self,
        room_creator: str | None = ...,
        is_public: bool | None = ...,
        room_version: str | None = ...,
        tok: str | None = ...,
        expect_code: int = ...,
        extra_content: dict | None = ...,
        custom_headers: Iterable[tuple[AnyStr, AnyStr]] | None = ...,
    ) -> str | None: ...

    def create_room_as(
        self,
        room_creator: str | None = None,
        is_public: bool | None = True,
        room_version: str | None = None,
        tok: str | None = None,
        expect_code: int = HTTPStatus.OK,
        extra_content: dict | None = None,
        custom_headers: Iterable[tuple[AnyStr, AnyStr]] | None = None,
    ) -> str | None:
        """
        Create a room.

        Args:
            room_creator: The user ID to create the room with.
            is_public: If True, the `visibility` parameter will be set to
                "public". If False, it will be set to "private".
                If None, doesn't specify the `visibility` parameter in which
                case the server is supposed to make the room private according to
                the CS API.
                Defaults to public, since that is commonly needed in tests
                for convenience where room privacy is not a problem.
            room_version: The room version to create the room as. Defaults to Synapse's
                default room version.
            tok: The access token to use in the request.
            expect_code: The expected HTTP response code.
            extra_content: Extra keys to include in the body of the /createRoom request.
                Note that if is_public is set, the "visibility" key will be overridden.
                If room_version is set, the "room_version" key will be overridden.
            custom_headers: HTTP headers to include in the request.

        Returns:
            The ID of the newly created room, or None if the request failed.
        """
        temp_id = self.auth_user_id
        self.auth_user_id = room_creator
        path = "/_matrix/client/r0/createRoom"
        content = extra_content or {}
        if is_public is not None:
            content["visibility"] = "public" if is_public else "private"
        if room_version:
            content["room_version"] = room_version
        if tok:
            path = f"{path}?access_token={tok}"

        channel = make_request(
            self.reactor,
            self.site,
            "POST",
            path,
            content,
            custom_headers=custom_headers,
        )

        assert channel.code == expect_code, channel.result
        self.auth_user_id = temp_id

        if expect_code == HTTPStatus.OK:
            return channel.json_body["room_id"]

        return None

    def invite(
        self,
        room: str,
        src: str | None = None,
        targ: str | None = None,
        expect_code: int = HTTPStatus.OK,
        tok: str | None = None,
    ) -> None:
        self.change_membership(
            room=room,
            src=src,
            targ=targ,
            tok=tok,
            membership=Membership.INVITE,
            expect_code=expect_code,
        )

    def join(
        self,
        room: str,
        user: str | None = None,
        expect_code: int = HTTPStatus.OK,
        tok: str | None = None,
        appservice_user_id: str | None = None,
        expect_errcode: Codes | None = None,
        expect_additional_fields: dict | None = None,
    ) -> None:
        self.change_membership(
            room=room,
            src=user,
            targ=user,
            tok=tok,
            appservice_user_id=appservice_user_id,
            membership=Membership.JOIN,
            expect_code=expect_code,
            expect_errcode=expect_errcode,
            expect_additional_fields=expect_additional_fields,
        )

    def knock(
        self,
        room: str | None = None,
        user: str | None = None,
        reason: str | None = None,
        expect_code: int = HTTPStatus.OK,
        tok: str | None = None,
    ) -> None:
        temp_id = self.auth_user_id
        self.auth_user_id = user
        path = f"/knock/{room}"
        if tok:
            path = f"{path}?access_token={tok}"

        data = {}
        if reason:
            data["reason"] = reason

        channel = make_request(
            self.reactor,
            self.site,
            "POST",
            path,
            data,
        )

        assert channel.code == expect_code, "Expected: %d, got: %d, resp: %r" % (
            expect_code,
            channel.code,
            channel.result["body"],
        )

        self.auth_user_id = temp_id

    def leave(
        self,
        room: str,
        user: str | None = None,
        expect_code: int = HTTPStatus.OK,
        tok: str | None = None,
    ) -> None:
        self.change_membership(
            room=room,
            src=user,
            targ=user,
            tok=tok,
            membership=Membership.LEAVE,
            expect_code=expect_code,
        )

    def ban(
        self,
        room: str,
        src: str,
        targ: str,
        expect_code: int = HTTPStatus.OK,
        tok: str | None = None,
    ) -> None:
        """A convenience helper: `change_membership` with `membership` preset to "ban"."""
        self.change_membership(
            room=room,
            src=src,
            targ=targ,
            tok=tok,
            membership=Membership.BAN,
            expect_code=expect_code,
        )

    def change_membership(
        self,
        room: str,
        src: str | None,
        targ: str | None,
        membership: str,
        extra_data: dict | None = None,
        tok: str | None = None,
        appservice_user_id: str | None = None,
        expect_code: int = HTTPStatus.OK,
        expect_errcode: str | None = None,
        expect_additional_fields: dict | None = None,
    ) -> None:
        """
        Send a membership state event into a room.

        Args:
            room: The ID of the room to send to
            src: The mxid of the event sender
            targ: The mxid of the event's target. The state key
            membership: The type of membership event
            extra_data: Extra information to include in the content of the event
            tok: The user access token to use
            appservice_user_id: The `user_id` URL parameter to pass.
                This allows driving an application service user
                using an application service access token in `tok`.
            expect_code: The expected HTTP response code
            expect_errcode: The expected Matrix error code
        """
        temp_id = self.auth_user_id
        self.auth_user_id = src

        path = f"/_matrix/client/r0/rooms/{room}/state/m.room.member/{targ}"
        url_params: dict[str, str] = {}

        if tok:
            url_params["access_token"] = tok

        if appservice_user_id:
            url_params["user_id"] = appservice_user_id

        if url_params:
            path += "?" + urlencode(url_params)

        data = {"membership": membership}
        data.update(extra_data or {})

        channel = make_request(
            self.reactor,
            self.site,
            "PUT",
            path,
            data,
        )

        assert channel.code == expect_code, "Expected: %d, got: %d, resp: %r" % (
            expect_code,
            channel.code,
            channel.result["body"],
        )

        if expect_errcode:
            assert (
                str(channel.json_body["errcode"]) == expect_errcode
            ), "Expected: {!r}, got: {!r}, resp: {!r}".format(
                expect_errcode,
                channel.json_body["errcode"],
                channel.result["body"],
            )

        if expect_additional_fields is not None:
            for expect_key, expect_value in expect_additional_fields.items():
                assert (
                    expect_key in channel.json_body
                ), f"Expected field {expect_key}, got {channel.json_body}"
                assert (
                    channel.json_body[expect_key] == expect_value
                ), f"Expected: {expect_value} at {expect_key}, got: {channel.json_body[expect_key]}, resp: {channel.json_body}"

        self.auth_user_id = temp_id

    def send(
        self,
        room_id: str,
        body: str | None = None,
        txn_id: str | None = None,
        tok: str | None = None,
        expect_code: int = HTTPStatus.OK,
        custom_headers: Iterable[tuple[AnyStr, AnyStr]] | None = None,
    ) -> JsonDict:
        if body is None:
            body = "body_text_here"

        content = {"msgtype": "m.text", "body": body}

        return self.send_event(
            room_id,
            "m.room.message",
            content,
            txn_id,
            tok,
            expect_code,
            custom_headers=custom_headers,
        )

    def send_event(
        self,
        room_id: str,
        type_: str,
        content: dict | None = None,
        txn_id: str | None = None,
        tok: str | None = None,
        expect_code: int = HTTPStatus.OK,
        custom_headers: Iterable[tuple[AnyStr, AnyStr]] | None = None,
    ) -> JsonDict:
        if txn_id is None:
            txn_id = f"m{time.time()!s}"

        path = f"/_matrix/client/r0/rooms/{room_id}/send/{type_}/{txn_id}"
        if tok:
            path = f"{path}?access_token={tok}"

        channel = make_request(
            self.reactor,
            self.site,
            "PUT",
            path,
            content or {},
            custom_headers=custom_headers,
        )

        assert channel.code == expect_code, "Expected: %d, got: %d, resp: %r" % (
            expect_code,
            channel.code,
            channel.result["body"],
        )

        return channel.json_body

    def get_event(
        self,
        room_id: str,
        event_id: str,
        tok: str | None = None,
        expect_code: int = HTTPStatus.OK,
    ) -> JsonDict:
        """Request a specific event from the server.

        Args:
            room_id: the room in which the event was sent.
            event_id: the event's ID.
            tok: the token to request the event with.
            expect_code: the expected HTTP status for the response.

        Returns:
            The event as a dict.
        """
        path = f"/_matrix/client/v3/rooms/{room_id}/event/{event_id}"
        if tok:
            path = path + f"?access_token={tok}"

        channel = make_request(
            self.reactor,
            self.site,
            "GET",
            path,
        )

        assert channel.code == expect_code, "Expected: %d, got: %d, resp: %r" % (
            expect_code,
            channel.code,
            channel.result["body"],
        )

        return channel.json_body

    def _read_write_state(
        self,
        room_id: str,
        event_type: str,
        body: dict[str, Any] | None,
        tok: str | None,
        expect_code: int = HTTPStatus.OK,
        state_key: str = "",
        method: str = "GET",
    ) -> JsonDict:
        """Read or write some state from a given room

        Args:
            room_id:
            event_type: The type of state event
            body: Body that is sent when making the request. The content of the state event.
                If None, the request to the server will have an empty body
            tok: The access token to use
            expect_code: The HTTP code to expect in the response
            state_key:
            method: "GET" or "PUT" for reading or writing state, respectively

        Returns:
            The response body from the server

        Raises:
            AssertionError: if expect_code doesn't match the HTTP code we received
        """
        path = f"/_matrix/client/r0/rooms/{room_id}/state/{event_type}/{state_key}"
        if tok:
            path = f"{path}?access_token={tok}"

        # Set request body if provided
        content = b""
        if body is not None:
            content = json.dumps(body).encode("utf8")

        channel = make_request(self.reactor, self.site, method, path, content)

        assert (
            channel.code == expect_code
        ), f"Expected: {expect_code}, got: {channel.code}, resp: {channel.result['body']}"

        return channel.json_body

    def get_state(
        self,
        room_id: str,
        event_type: str,
        tok: str,
        expect_code: int = HTTPStatus.OK,
        state_key: str = "",
    ) -> JsonDict:
        """Gets some state from a room

        Args:
            room_id:
            event_type: The type of state event
            tok: The access token to use
            expect_code: The HTTP code to expect in the response
            state_key:

        Returns:
            The response body from the server

        Raises:
            AssertionError: if expect_code doesn't match the HTTP code we received
        """
        return self._read_write_state(
            room_id, event_type, None, tok, expect_code, state_key, method="GET"
        )

    def send_state(
        self,
        room_id: str,
        event_type: str,
        body: dict[str, Any],
        tok: str | None,
        expect_code: int = HTTPStatus.OK,
        state_key: str = "",
    ) -> JsonDict:
        """Set some state in a room

        Args:
            room_id:
            event_type: The type of state event
            body: Body that is sent when making the request. The content of the state event.
            tok: The access token to use
            expect_code: The HTTP code to expect in the response
            state_key:

        Returns:
            The response body from the server

        Raises:
            AssertionError: if expect_code doesn't match the HTTP code we received
        """
        return self._read_write_state(
            room_id, event_type, body, tok, expect_code, state_key, method="PUT"
        )
