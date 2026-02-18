from http import HTTPStatus

from synapse import event_auth
from synapse.api.constants import (
    CREATOR_POWER_LEVEL,
    EventTypes,
)
from synapse.server import HomeServer
from synapse.types.state import StateFilter
from synapse.util.clock import Clock
from twisted.internet.testing import MemoryReactor

from famedly_control_synapse.rest.room import MANAGED_ROOM_TYPE
from famedly_control_synapse.types import CreateManagedRoomRequest, CreationContent
from tests.utils.module_api_testcase import ModuleApiTestCase

CREATE_KEY = (EventTypes.Create, "")
POWER_KEY = (EventTypes.PowerLevels, "")


class TestManagedRoomCreation(ModuleApiTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer):
        super().prepare(reactor, clock, homeserver)

    def room_config_v12(self):
        config = CreateManagedRoomRequest(
            room_alias_name="test_room_alias",
            name="Test Room",
            topic="This is a test room",
            groups=["test_group"],
        )
        return config.model_dump()

    def room_config_v10(self):
        config = CreateManagedRoomRequest(
            room_alias_name="test_room_alias",
            name="Test Room",
            room_version="10",
            creation_content=CreationContent(creator=self.creator, room_version="10"),
            topic="This is a test room",
            groups=["test_group"],
        )
        return config.model_dump()

    def invalid_room_config(self):
        config = {
            "room_alias_name": "test_room_alias",
            "name": "Test Room",
            "topic": "This is a test room",
            "3pid_invites": ["something"],  # Invalid field
        }
        return config

    def test_room_creation_success(self) -> None:
        """Tests that managed room creation returns the expected response"""
        channel = self.make_request(
            method="POST",
            path="/_famedlyControl/v1/managedRooms/createRoom",
            content=self.room_config_v12(),
            access_token=self.creator_access_token,
            shorthand=False,
        )

        assert channel.code == HTTPStatus.OK, channel.result
        assert "room_id" in channel.json_body, "Response should contain room_id"
        room_id = channel.json_body["room_id"]

        # Check if the room has the correct configuration
        state_map = self.get_success(
            self.hs.get_storage_controllers().state.get_current_state(
                room_id,
                StateFilter.from_types(
                    [(EventTypes.GuestAccess, None), (EventTypes.JoinRules, None)]
                ),
            )
        )
        assert state_map[EventTypes.JoinRules, ""].content == {
            "join_rule": "invite"
        }, "Join rules should be set to invite"
        assert state_map[EventTypes.GuestAccess, ""].content == {
            "guest_access": "forbidden"
        }, "Guest access should be set to forbidden"

        # Check if account data is updated
        account_data = self.get_success(
            self.store.get_account_data_for_room(self.creator, room_id)
        )
        assert account_data == {MANAGED_ROOM_TYPE: {"groups": ["test_group"]}}

    def test_room_creation_invalid_body(self) -> None:
        """Tests that invalid request body returns a 400 error with an error message"""
        channel = self.make_request(
            method="POST",
            path="/_famedlyControl/v1/managedRooms/createRoom",
            content=self.invalid_room_config(),
            access_token=self.creator_access_token,
            shorthand=False,
        )

        assert channel.code == HTTPStatus.BAD_REQUEST, channel.result
        assert (
            "Invalid request body" in channel.json_body["error"]
        ), "Response should contain an error message"

    def test_room_creation_with_missing_required_fields(self) -> None:
        """Tests that missing required fields in the request body returns a 400 error with an error message"""
        channel = self.make_request(
            method="POST",
            path="/_famedlyControl/v1/managedRooms/createRoom",
            content={"name": "Test Room"},
            access_token=self.creator_access_token,
            shorthand=False,
        )

        assert channel.code == HTTPStatus.BAD_REQUEST, channel.result
        assert (
            "Invalid request body" in channel.json_body["error"]
        ), "Response should contain an error message"

    def test_room_creation_powerlevel_with_room_v12(self) -> None:
        """Tests that the creator has the highest power level and no other user can have the same"""
        room_config = self.room_config_v12()
        channel = self.make_request(
            method="POST",
            path="/_famedlyControl/v1/managedRooms/createRoom",
            content=room_config,
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        room_id = channel.json_body["room_id"]

        # Check if the creator has ultimate power level and no other user has the same power level
        auth_events = self.get_success(
            self.hs.get_storage_controllers().state.get_current_state(
                room_id,
                StateFilter.from_types(
                    [
                        POWER_KEY,
                        CREATE_KEY,
                    ]
                ),
            )
        )
        creator_pl = event_auth.get_user_power_level(self.creator, auth_events)
        assert (
            creator_pl == 9007199254740992
        ), "Creator should have power level 9007199254740992"

        # Invite a user
        self.helper.invite(
            room=room_id,
            src=self.creator,
            targ=self.invitee,
            tok=self.creator_access_token,
            expect_code=200,
        )
        invitee_pl = event_auth.get_user_power_level(self.invitee, auth_events)
        assert invitee_pl == 0, "Invitee should have power level 0"

    def test_room_creation_powerlevel_with_room_v10(self) -> None:
        room_config = self.room_config_v10()
        channel = self.make_request(
            method="POST",
            path="/_famedlyControl/v1/managedRooms/createRoom",
            content=room_config,
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        room_id = channel.json_body["room_id"]

        # Check if the creator has infinite power level and no other user has the same power level
        auth_events = self.get_success(
            self.hs.get_storage_controllers().state.get_current_state(
                room_id,
                StateFilter.from_types(
                    [
                        POWER_KEY,
                        CREATE_KEY,
                    ]
                ),
            )
        )
        creator_pl = event_auth.get_user_power_level(self.creator, auth_events)
        assert (
            creator_pl == CREATOR_POWER_LEVEL - 1
        ), "Creator should have infinite power level"

        # Invite user
        self.helper.invite(
            room=room_id,
            src=self.creator,
            targ=self.invitee,
            tok=self.creator_access_token,
            expect_code=200,
        )
        invitee_pl = event_auth.get_user_power_level(self.invitee, auth_events)
        assert invitee_pl == 0, "Invitee should have power level 0"
