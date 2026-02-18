from http import HTTPStatus

from synapse.api.constants import (
    EventTypes,
)
from synapse.server import HomeServer
from synapse.types.state import StateFilter
from synapse.util.clock import Clock
from twisted.internet.testing import MemoryReactor

from famedly_control_synapse.types import CreateManagedRoomRequest
from tests.utils.module_api_testcase import ModuleApiTestCase


class TestManagedRoomCreation(ModuleApiTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer):
        super().prepare(reactor, clock, homeserver)

    def room_config(self, group_id: str | None = None):
        config = CreateManagedRoomRequest(
            room_alias_name="test_room_alias",
            name="Test Room",
            topic="This is a test room",
        )
        if group_id:
            config.groups = [group_id]

        # logging.error("Room Config Object %s", config)

        return config.model_dump()

    def test_room_creation_success(self) -> None:
        """Tests that managed room creation returns the expected response"""
        channel = self.make_request(
            method="POST",
            path="/_famedlyControl/v1/managedRooms/createRoom",
            content=self.room_config(),
            access_token=self.creator_access_token,
            shorthand=False,
        )

        assert channel.code == HTTPStatus.OK, channel.result
        assert "room_id" in channel.json_body, "Response should contain room_id"

        # Check if the room has the correct configuration (join rules, power levels, etc.)
        room_id = channel.json_body["room_id"]

        # TODO: do creator test with room v10 and below
        # channel = self.make_request(
        #     method="GET",
        #     path=f"/_matrix/client/v3/rooms/{room_id}/state/m.room.create",
        #     access_token=self.creator_access_token,
        # )
        # assert channel.code == HTTPStatus.OK, channel.result
        # logging.error("Guest Access State Event: %s", channel.json_body)
        # assert channel.json_body["creator"] == self.creator, channel.json_body["creator"]

        channel = self.make_request(
            method="GET",
            path=f"/_matrix/client/v3/rooms/{room_id}/state/m.room.join_rules",
            access_token=self.creator_access_token,
        )
        assert channel.code == HTTPStatus.OK, channel.result

        assert channel.json_body["join_rule"] == "invite", channel.json_body[
            "join_rule"
        ]

        channel = self.make_request(
            method="GET",
            path=f"/_matrix/client/v3/rooms/{room_id}/state/m.room.guest_access",
            access_token=self.creator_access_token,
        )
        assert channel.code == HTTPStatus.OK, channel.result

        assert channel.json_body["guest_access"] == "forbidden", channel.json_body[
            "guest_access"
        ]

        # room_data = self.get_success(
        #     self.hs.get_message_handler().get_room_data(
        #         self.requester, room_id, "m.room.join_rules", ""
        #     )
        # )
        # logging.error("room_data: %s", room_data.items())

        room_stats = self.get_success(
            self.hs.get_storage_controllers().state.get_current_state(
                room_id,
                StateFilter.from_types(
                    [(EventTypes.GuestAccess, None), (EventTypes.JoinRules, None)]
                ),
            )
        )
        # logging.error("room_stats: %s", room_stats.items())
        # logging.error("JoinRules: %s", dict(room_stats[EventTypes.JoinRules, ""]))

        assert room_stats[EventTypes.JoinRules, ""].content == {
            "join_rule": "invite"
        }, "Join rules should be set to invite"
        assert room_stats[EventTypes.GuestAccess, ""].content == {
            "guest_access": "forbidden"
        }, "Guest access should be set to forbidden"

    def invalid_room_config(self, group_id: str | None = None):
        config = {
            "room_alias_name": "test_room_alias",
            "name": "Test Room",
            "topic": "This is a test room",
            "3pid_invites": [],
        }
        if group_id:
            config["groups"] = [group_id]
        # logging.error("Room Config Object %s", config)
        return config

    # def test_room_creation_invalid_body(self) -> None:
    #     """Tests that invalid request body returns a 400 error with an error message"""
    #     channel = self.make_request(
    #         method="POST",
    #         path="/_famedlyControl/v1/managedRooms/createRoom",
    #         content=self.invalid_room_config(),
    #         access_token=self.creator_access_token,
    #         shorthand=False,
    #     )

    #     assert channel.code == HTTPStatus.BAD_REQUEST, channel.result
    #     assert "error" in channel.json_body, "Response should contain an error message"

    # TODO: Add tests for the room creation endpoint, including:
    # POST with invalid body returns 400 with an error message
    # POST with missing required fields returns 400 with an error message
    # POST with invalid creator format returns 400 with an error message or creator missing for room version <= 10
    # POST with different room versions
    # POST creates a room with the different and acceptable configuration (join rules, power levels, etc.)
    # Check if the created room has the correct account data event with the groups field populated correctly
