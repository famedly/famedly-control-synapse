from http import HTTPStatus
from unittest.mock import AsyncMock, patch

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
            path=self.CREATE_PATH,
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
            path=self.CREATE_PATH,
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
            path=self.CREATE_PATH,
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
            path=self.CREATE_PATH,
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
            path=self.CREATE_PATH,
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

    @patch(
        "famedly_control_synapse.client.FamedlyControlClient.get_group_members",
        new_callable=AsyncMock,
    )
    def test_room_created_with_memebers_joined(self, mock_get_group_members) -> None:
        """Tests that the users of the groups are joined to the room after creation"""
        test_group = "test_group_1"
        test_member_1 = self.register_user("test_member_1", "password")
        test_member_2 = self.register_user("test_member_2", "password")
        group_members = [test_member_1, test_member_2]
        mock_get_group_members.return_value = group_members

        # Create a managed room with a group that has members
        room_id = self._create_managed_room(
            name="Test Room with Group Members", groups=[test_group]
        )

        # Check if the member of the group is joined to the room
        for member in group_members:
            path = f"/_matrix/client/v3/rooms/{room_id}/state/m.room.member/{member}"
            channel = self.make_request(
                "GET", path, access_token=self.creator_access_token
            )
            assert (
                channel.code == HTTPStatus.OK
            ), f"Expected 200 but got {channel.code} for member {member}"
            assert channel.json_body["membership"] == "join", channel.json_body[
                "membership"
            ]

        # Check the account data is updated
        room_account_data = self.get_success(
            self.store.get_account_data_for_room(self.creator, room_id)
        )
        assert room_account_data == {
            MANAGED_ROOM_TYPE: {"groups": [test_group]}
        }, room_account_data


class TestAssignGroupsToManagedRoom(ModuleApiTestCase):
    @patch(
        "famedly_control_synapse.client.FamedlyControlClient.get_group_members",
        new_callable=AsyncMock,
    )
    def test_update_group_to_managed_room(self, mock_get_group_members) -> None:
        """Tests that managed room which already have groups can be updated and the
        account data is updated correctly."""

        test_old_group = "test_old_group"
        test_member_1 = self.register_user("test_member_1", "password")
        test_member_2 = self.register_user("test_member_2", "password")
        old_group_members = [test_member_1, test_member_2]

        test_new_group = "test_new_group"
        test_member_3 = self.register_user("test_member_3", "password")
        new_group_members = [test_member_2, test_member_3]

        # Configure mock to return different values based on group_id
        def get_members_by_group(group_id):
            if group_id == test_old_group:
                return old_group_members
            elif group_id == test_new_group:
                return new_group_members
            return []

        mock_get_group_members.side_effect = get_members_by_group

        room_id = self._create_managed_room(
            name="Test Room with Group Members", groups=[test_old_group]
        )
        self._test_get_membership(room_id, old_group_members, expect_code=200)
        room_account_data = self.get_success(
            self.store.get_account_data_for_room(self.creator, room_id)
        )
        assert room_account_data == {
            MANAGED_ROOM_TYPE: {"groups": [test_old_group]}
        }, room_account_data

        # Now update the new group
        channel = self.make_request(
            method="POST",
            path=self.BASE_PATH + f"/{room_id}/groups",
            content={"groups": [test_new_group]},
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result

        # Check if the new member of the group is joined to the room
        for member in new_group_members:
            path = f"/_matrix/client/v3/rooms/{room_id}/state/m.room.member/{member}"
            channel = self.make_request(
                "GET", path, access_token=self.creator_access_token
            )
            assert (
                channel.code == HTTPStatus.OK
            ), f"Expected 200 but got {channel.code} for member {member}"
            assert (
                channel.json_body["membership"] == "join"
            ), f"Expected membership to be join but got {channel.json_body['membership']} for member {member}"

        # Check if the old member who is not in the new group is removed from the room
        path = f"/_matrix/client/v3/rooms/{room_id}/state/m.room.member/{test_member_1}"
        channel = self.make_request("GET", path, access_token=self.creator_access_token)
        assert (
            channel.code == HTTPStatus.OK
        ), f"Expected 200 but got {channel.code} for member {test_member_1}"
        assert (
            channel.json_body["membership"] == "leave"
        ), f"Expected membership to be leave but got {channel.json_body['membership']} for member {test_member_1}"

        # Check the account data is updated
        room_account_data = self.get_success(
            self.store.get_account_data_for_room(self.creator, room_id)
        )
        assert room_account_data == {
            MANAGED_ROOM_TYPE: {"groups": [test_new_group]}
        }, room_account_data


class TestListManagedRooms(ModuleApiTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer):
        super().prepare(reactor, clock, homeserver)
        self.non_admin = self.register_user("non_admin", "password", admin=False)
        self.non_admin_token = self.login("non_admin", "password")
        self.account_data_handler = homeserver.get_account_data_handler()

    def test_list_requires_admin(self) -> None:
        """Non-admin users should get a 403."""
        channel = self.make_request(
            method="GET",
            path=self.LIST_PATH,
            access_token=self.non_admin_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.FORBIDDEN, channel.result

    def test_list_requires_auth(self) -> None:
        """Unauthenticated requests should get a 401."""
        channel = self.make_request(
            method="GET",
            path=self.LIST_PATH,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.UNAUTHORIZED, channel.result

    def test_list_empty(self) -> None:
        """Listing when no managed rooms exist should return empty chunk."""
        channel = self.make_request(
            method="GET",
            path=self.LIST_PATH,
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        assert channel.json_body["chunk"] == []
        assert channel.json_body["total_room_count_estimate"] == 0

    def test_list_returns_managed_rooms(self) -> None:
        """Created managed rooms should appear in the listing."""
        room_id = self._create_managed_room(
            name="Listed Room", groups=["group1", "group2"]
        )

        channel = self.make_request(
            method="GET",
            path=self.LIST_PATH,
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        assert channel.json_body["total_room_count_estimate"] == 1
        assert len(channel.json_body["chunk"]) == 1

        room_chunk = channel.json_body["chunk"][0]
        assert room_chunk["room_id"] == room_id
        assert room_chunk["de.famedly.managedRoom"]["groups"] == [
            "group1",
            "group2",
        ]

    def test_list_pagination(self) -> None:
        """Pagination should work with from and limit params."""
        room_ids = []
        for i in range(3):
            room_ids.append(self._create_managed_room(name=f"Room {i}"))

        # Get first page with limit 2
        channel = self.make_request(
            method="GET",
            path=f"{self.LIST_PATH}?limit=2",
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        assert len(channel.json_body["chunk"]) == 2
        assert channel.json_body["total_room_count_estimate"] == 3
        assert "next_batch" in channel.json_body
        assert "prev_batch" not in channel.json_body

        # Get second page
        next_batch = channel.json_body["next_batch"]
        channel = self.make_request(
            method="GET",
            path=f"{self.LIST_PATH}?from={next_batch}&limit=2",
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        assert len(channel.json_body["chunk"]) == 1
        assert "next_batch" not in channel.json_body
        assert "prev_batch" in channel.json_body

    def test_list_no_duplicates_with_multiple_users(self) -> None:
        """A room with account data from multiple users should appear once
        with all groups merged."""
        from famedly_control_synapse.types import MANAGED_ROOM_TYPE

        room_id = self._create_managed_room(name="Shared Room", groups=["group_a"])

        # Register extra users with overlapping groups
        user_groups = [
            ["group_a", "group_b"],  # overlaps with creator's group_a
            ["group_b", "group_c"],  # overlaps with previous user's group_b
            ["group_a", "group_c"],  # overlaps with both creator and user above
        ]
        for i, groups in enumerate(user_groups):
            user = self.register_user(f"extra_user_{i}", "password", admin=False)
            self.get_success(
                self.account_data_handler.add_account_data_to_room(
                    user,
                    room_id,
                    MANAGED_ROOM_TYPE,
                    {"groups": groups},
                )
            )

        channel = self.make_request(
            method="GET",
            path=self.LIST_PATH,
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        assert (
            channel.json_body["total_room_count_estimate"] == 1
        ), f"Expected 1 room but count was {channel.json_body['total_room_count_estimate']}"
        assert (
            len(channel.json_body["chunk"]) == 1
        ), f"Expected 1 entry in chunk but got {len(channel.json_body['chunk'])}"

        result_groups = channel.json_body["chunk"][0]["de.famedly.managedRoom"][
            "groups"
        ]
        assert sorted(result_groups) == [
            "group_a",
            "group_b",
            "group_c",
        ], f"Expected deduplicated merged groups, got {result_groups}"
