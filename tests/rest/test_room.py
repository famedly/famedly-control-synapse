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
from famedly_control_synapse.rest.types import CreateManagedRoomRequest, CreationContent
from tests.utils.module_api_testcase import ModuleApiTestCase

CREATE_KEY = (EventTypes.Create, "")
POWER_KEY = (EventTypes.PowerLevels, "")


class TestManagedRoomCreation(ModuleApiTestCase):
    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        super().prepare(reactor=reactor, clock=clock, homeserver=homeserver)
        # This test series has one group with id 'test_group' and one member
        self.fc_rest_helper.create_group("test_group", [self.invitee])

    def room_config_v12(self):
        config = CreateManagedRoomRequest(
            room_alias_name="test_room_alias",
            name="Test Room",
            room_version="12",
            topic="This is a test room",
            groups=["test_group"],
        )
        return config.model_dump()

    def room_config_v10(self):
        config = CreateManagedRoomRequest(
            room_alias_name="test_room_alias",
            name="Test Room",
            room_version="10",
            creation_content=CreationContent(creator=self.creator),
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
        channel = self.fc_rest_helper.create_managed_room(
            content=self.room_config_v12(),
            access_token=self.creator_access_token,
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
        channel = self.fc_rest_helper.create_managed_room(
            content=self.invalid_room_config(),
            access_token=self.creator_access_token,
        )

        assert channel.code == HTTPStatus.BAD_REQUEST, channel.result
        assert (
            "Invalid request body" in channel.json_body["error"]
        ), "Response should contain an error message"

    def test_room_creation_with_missing_required_fields(self) -> None:
        """Tests that missing required fields in the request body returns a 400 error with an error message"""
        channel = self.fc_rest_helper.create_managed_room(
            content={"name": "Test Room"},
            access_token=self.creator_access_token,
        )

        assert channel.code == HTTPStatus.BAD_REQUEST, channel.result
        assert (
            "Invalid request body" in channel.json_body["error"]
        ), "Response should contain an error message"

    def test_room_creation_powerlevel_with_room_v12(self) -> None:
        """Tests that the creator has the highest power level and no other user can have the same"""
        room_config = self.room_config_v12()
        channel = self.fc_rest_helper.create_managed_room(
            content=room_config,
            access_token=self.creator_access_token,
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

        # Check the invited user's power level
        invitee_pl = event_auth.get_user_power_level(self.invitee, auth_events)
        assert invitee_pl == 0, "Invitee should have power level 0"

    def test_room_creation_powerlevel_with_room_v10(self) -> None:
        room_config = self.room_config_v10()
        channel = self.fc_rest_helper.create_managed_room(
            content=room_config,
            access_token=self.creator_access_token,
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

        # Check the invited user's power level
        invitee_pl = event_auth.get_user_power_level(self.invitee, auth_events)
        assert invitee_pl == 0, "Invitee should have power level 0"

    def test_room_created_with_members_joined(self) -> None:
        """Tests that the users of the groups are joined to the room after creation"""
        test_member_1 = self.register_user("test_member_1", "password")
        test_member_2 = self.register_user("test_member_2", "password")
        self.get_success(self.fc_rest_helper.register_external_id(test_member_1))
        self.get_success(self.fc_rest_helper.register_external_id(test_member_2))

        test_group = "test_group_1"
        group_members = [test_member_1, test_member_2]

        self.fc_rest_helper.create_group(test_group, group_members)

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


@patch(
    "famedly_control_synapse.client.FamedlyControlClient.get_group_members",
    new_callable=AsyncMock,
)
@patch(
    "famedly_control_synapse.room_handler.ManagedRoomHandler.batch_convert_external_user_ids_to_matrix_user_ids",
    new_callable=AsyncMock,
)
class TestAssignGroupsToManagedRoom(ModuleApiTestCase):

    def test_update_single_group_to_single_group(
        self, mock_batch_convert, mock_get_group_members
    ) -> None:
        """Tests that managed room which already have groups can be updated and the
        account data is updated correctly."""
        test_old_group = "test_old_group"
        test_member_1 = self.register_user("test_member_1", "password")
        test_member_2 = self.register_user("test_member_2", "password")
        old_group_members = [test_member_1, test_member_2]

        test_new_group = "test_new_group"
        test_member_3 = self.register_user("test_member_3", "password")
        new_group_members = [test_member_2, test_member_3]

        def get_group_members(group_id):  # in real case this should return external_ids
            if group_id == test_old_group:
                return old_group_members
            elif group_id == test_new_group:
                return new_group_members
            return []

        mock_get_group_members.side_effect = get_group_members
        mock_batch_convert.side_effect = lambda x: (x, [])

        # Create a managed room with the old group information
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

        # Now update the group information
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

    def test_update_single_group_to_multiple_groups(
        self, mock_batch_convert, mock_get_group_members
    ) -> None:
        """
        Test updating a single group to multiple groups in a managed room.
        Old groups info: test_group_1
        New groups info: test_group_1, test_group_2, test_group_3
        Edge case to consider with member r who was in group 1 but removed from group later.
        """
        test_group_1 = "test_group_1"
        test_member_a = self.register_user("test_member_a", "password")
        test_member_b = self.register_user("test_member_b", "password")
        test_member_r = self.register_user("test_member_r", "password")
        test_group_1_current_members = [test_member_a, test_member_b]
        test_group_1_creation_members = [test_member_a, test_member_b, test_member_r]

        test_group_2 = "test_group_2"
        test_member_c = self.register_user("test_member_c", "password")
        test_group_2_members = [test_member_b, test_member_c]

        test_group_3 = "test_group_3"
        test_member_d = self.register_user("test_member_d", "password")
        test_member_e = self.register_user("test_member_e", "password")
        test_group_3_members = [test_member_d, test_member_e]

        old_group_info = [test_group_1]
        new_group_info = [test_group_1, test_group_2, test_group_3]

        call_counts = {}

        def get_members_by_group(
            group_id,
        ):  # in real case this should return external_ids
            if group_id not in call_counts:
                call_counts[group_id] = 0
            # Track how many times each group_id has been called
            call_counts[group_id] += 1

            if group_id == test_group_1:
                # First call returns creation members, subsequent calls return current members
                if call_counts[group_id] == 1:
                    return test_group_1_creation_members
                else:
                    return test_group_1_current_members
            elif group_id == test_group_2:
                return test_group_2_members
            elif group_id == test_group_3:
                return test_group_3_members
            return []

        mock_get_group_members.side_effect = get_members_by_group
        mock_batch_convert.side_effect = lambda x: (x, [])

        # Create a managed room with the old group information
        room_id = self._create_managed_room(
            name="Test Room with Group Members", groups=old_group_info
        )

        self._test_get_membership(
            room_id, test_group_1_creation_members, expect_code=200
        )
        room_account_data = self.get_success(
            self.store.get_account_data_for_room(self.creator, room_id)
        )
        assert room_account_data == {
            MANAGED_ROOM_TYPE: {"groups": old_group_info}
        }, room_account_data

        # Now update the group information
        channel = self.make_request(
            method="POST",
            path=self.BASE_PATH + f"/{room_id}/groups",
            content={"groups": new_group_info},
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result

        member_should_be_in_room = set(
            test_group_1_current_members + test_group_2_members + test_group_3_members
        )
        member_should_not_be_in_room = [test_member_r]

        # Check if the new member of the group is joined to the room
        for member in member_should_be_in_room:
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

        for member in member_should_not_be_in_room:
            path = f"/_matrix/client/v3/rooms/{room_id}/state/m.room.member/{member}"
            channel = self.make_request(
                "GET", path, access_token=self.creator_access_token
            )
            assert (
                channel.code == HTTPStatus.OK
            ), f"Expected 200 but got {channel.code} for member {member}"
            assert (
                channel.json_body["membership"] == "leave"
            ), f"Expected membership to be leave but got {channel.json_body['membership']} for member {member}"

        # Check the account data is updated
        room_account_data = self.get_success(
            self.store.get_account_data_for_room(self.creator, room_id)
        )
        assert room_account_data == {
            MANAGED_ROOM_TYPE: {"groups": new_group_info}
        }, room_account_data

    def test_update_multiple_groups_to_multiple_groups(
        self, mock_batch_convert, mock_get_group_members
    ) -> None:
        """
        Test updating multiple groups to multiple groups in a managed room.
        Old groups info: test_group_1, test_group_2, test_group_3
        New groups info: test_group_1, test_group_4
        The edge cases to consider with member_r and member_b who are in multiple groups.
        """
        test_group_1 = "test_group_1"
        test_member_a = self.register_user("test_member_a", "password")
        test_member_b = self.register_user("test_member_b", "password")
        test_member_r = self.register_user("test_member_r", "password")
        test_group_1_current_members = [test_member_a, test_member_b]
        test_group_1_creation_members = [test_member_a, test_member_b, test_member_r]

        test_group_2 = "test_group_2"
        # Edge case example: member b is in both group 1 and 2. Group 1 is staying in
        # the room and group 2 is removed from the room.
        # member b should stay
        test_member_c = self.register_user("test_member_c", "password")
        test_group_2_members = [test_member_b, test_member_c]

        test_group_3 = "test_group_3"
        test_member_d = self.register_user("test_member_d", "password")
        test_member_e = self.register_user("test_member_e", "password")
        test_group_3_members = [test_member_d, test_member_e]

        test_group_4 = "test_group_4"
        # Edge case example: member r is removed from group 1, but added by group 4
        # and the group 1 remains to the room and group 4 is added to the room.
        # member r should stay
        test_member_f = self.register_user("test_member_f", "password")
        test_group_4_members = [test_member_f, test_member_r]

        old_group_info = [test_group_1, test_group_2, test_group_3]
        new_group_info = [test_group_1, test_group_4]

        call_counts = {}

        def get_members_by_group(
            group_id,
        ):  # in real case this should return external_ids
            if group_id not in call_counts:
                call_counts[group_id] = 0
            call_counts[group_id] += 1

            if group_id == test_group_1:
                # First call returns creation members, subsequent calls return current members
                if call_counts[group_id] == 1:
                    return test_group_1_creation_members
                else:
                    return test_group_1_current_members
            elif group_id == test_group_2:
                return test_group_2_members
            elif group_id == test_group_3:
                return test_group_3_members
            elif group_id == test_group_4:
                return test_group_4_members
            return []

        mock_get_group_members.side_effect = get_members_by_group
        mock_batch_convert.side_effect = lambda x: (x, [])

        # Create a managed room with the old group information
        room_id = self._create_managed_room(
            name="Test Room with Group Members", groups=old_group_info
        )
        self._test_get_membership(
            room_id,
            test_group_1_creation_members + test_group_2_members + test_group_3_members,
            expect_code=200,
        )
        room_account_data = self.get_success(
            self.store.get_account_data_for_room(self.creator, room_id)
        )
        assert room_account_data == {
            MANAGED_ROOM_TYPE: {"groups": old_group_info}
        }, room_account_data

        # Now update the group information
        channel = self.make_request(
            method="POST",
            path=self.BASE_PATH + f"/{room_id}/groups",
            content={"groups": new_group_info},
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result

        member_should_be_in_room = test_group_1_current_members + test_group_4_members
        member_should_not_be_in_room = [test_member_c] + test_group_3_members

        # Check if the new member of the group is joined to the room
        for member in member_should_be_in_room:
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

        for member in member_should_not_be_in_room:
            path = f"/_matrix/client/v3/rooms/{room_id}/state/m.room.member/{member}"
            channel = self.make_request(
                "GET", path, access_token=self.creator_access_token
            )
            assert (
                channel.code == HTTPStatus.OK
            ), f"Expected 200 but got {channel.code} for member {member}"
            assert (
                channel.json_body["membership"] == "leave"
            ), f"Expected membership to be leave but got {channel.json_body['membership']} for member {member}"

        # Check the account data is updated
        room_account_data = self.get_success(
            self.store.get_account_data_for_room(self.creator, room_id)
        )
        assert room_account_data == {
            MANAGED_ROOM_TYPE: {"groups": new_group_info}
        }, room_account_data

    def test_prevent_room_creator_membership_change(
        self, mock_batch_convert, mock_get_group_members
    ) -> None:
        """Tests that the creator's membership is not changed even if the creator is in the group and the group is removed from the room."""
        group_including_creator = "test_group_including_creator"
        member_1 = self.register_user("test_member_1", "password")
        member_2 = self.register_user("test_member_2", "password")
        group_including_creator_members = [self.creator, member_1, member_2]

        new_group = "new_group"
        member_3 = self.register_user("test_member_3", "password")
        new_group_members = [member_3]

        def get_group_members(group_id):  # in real case this should return external_ids
            if group_id == group_including_creator:
                return group_including_creator_members
            elif group_id == new_group:
                return new_group_members
            return []

        mock_get_group_members.side_effect = get_group_members
        mock_batch_convert.side_effect = lambda x: (x, [])

        # Create a managed room with a group that has the creator as a member
        room_id = self._create_managed_room(
            name="Test Room with Group Members", groups=[group_including_creator]
        )

        # Now update the group information to remove the group from the room
        channel = self.make_request(
            method="POST",
            path=self.BASE_PATH + f"/{room_id}/groups",
            content={"groups": [new_group]},
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result

        # Check if the creator is still joined to the room
        path = f"/_matrix/client/v3/rooms/{room_id}/state/m.room.member/{self.creator}"
        channel = self.make_request("GET", path, access_token=self.creator_access_token)
        assert (
            channel.code == HTTPStatus.OK
        ), f"Expected 200 but got {channel.code} for member {self.creator}"
        assert (
            channel.json_body["membership"] == "join"
        ), f"Expected membership to be join but got {channel.json_body['membership']} for member {self.creator}"


@patch(
    "famedly_control_synapse.client.FamedlyControlClient.get_group_members",
    new_callable=AsyncMock,
)
@patch(
    "famedly_control_synapse.room_handler.ManagedRoomHandler.batch_convert_external_user_ids_to_matrix_user_ids",
    new_callable=AsyncMock,
)
class TestListManagedRooms(ModuleApiTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer):
        super().prepare(reactor, clock, homeserver)
        self.non_admin = self.register_user("non_admin", "password", admin=False)
        self.non_admin_token = self.login("non_admin", "password")
        self.account_data_handler = homeserver.get_account_data_handler()

    def test_list_requires_admin(
        self, mock_batch_convert, mock_get_group_members
    ) -> None:
        """Non-admin users should get a 403."""
        channel = self.make_request(
            method="GET",
            path=self.LIST_PATH,
            access_token=self.non_admin_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.FORBIDDEN, channel.result

    def test_list_requires_auth(
        self, mock_batch_convert, mock_get_group_members
    ) -> None:
        """Unauthenticated requests should get a 401."""
        channel = self.make_request(
            method="GET",
            path=self.LIST_PATH,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.UNAUTHORIZED, channel.result

    def test_list_empty(self, mock_batch_convert, mock_get_group_members) -> None:
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

    def test_list_returns_managed_rooms(
        self, mock_batch_convert, mock_get_group_members
    ) -> None:
        """Created managed rooms should appear in the listing."""
        user_1 = self.register_user("user1", "password")
        user_2 = self.register_user("user2", "password")

        def get_members_by_group(
            group_id,
        ):  # in real case this should return external_ids
            if group_id == "group1":
                return [user_1]
            if group_id == "group2":
                return [user_2]
            return []

        mock_get_group_members.side_effect = get_members_by_group
        mock_batch_convert.side_effect = lambda x: (x, [])

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

    def test_list_pagination(self, mock_batch_convert, mock_get_group_members) -> None:
        """Pagination should work with from and limit params."""
        user_1 = self.register_user("user1", "password")
        user_2 = self.register_user("user2", "password")
        user_3 = self.register_user("user3", "password")

        def get_members_by_group(
            group_id,
        ):  # in real case this should return external_ids
            if group_id == "group1":
                return [user_1]
            if group_id == "group2":
                return [user_2]
            if group_id == "group3":
                return [user_3]
            return []

        mock_get_group_members.side_effect = get_members_by_group
        mock_batch_convert.side_effect = lambda x: (x, [])

        room_ids = []
        for i in range(3):
            room_ids.append(
                self._create_managed_room(name=f"Room {i}", groups=[f"group{i}"])
            )

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
