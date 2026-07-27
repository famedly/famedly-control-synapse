from http import HTTPStatus
from unittest.mock import AsyncMock, patch

from parameterized import parameterized, parameterized_class
from synapse import event_auth
from synapse.api.constants import (
    CREATOR_POWER_LEVEL,
    EventTypes,
    GuestAccess,
    Membership,
)
from synapse.api.room_versions import KNOWN_ROOM_VERSIONS
from synapse.module_api.errors import Codes, SynapseError
from synapse.server import HomeServer
from synapse.types import JsonDict, StateMap
from synapse.types.state import StateFilter
from synapse.util.clock import Clock
from twisted.internet.testing import MemoryReactor

from famedly_control_synapse.client import FamedlyControlError
from famedly_control_synapse.rest.types import CreateManagedRoomRequest, CreationContent
from famedly_control_synapse.room_handler import famedly_control_user_sync_error
from famedly_control_synapse.types import MANAGED_ROOM_TYPE, ActionReason
from tests.utils.homeserver_testcase import override_config
from tests.utils.module_api_testcase import ModuleApiTestCase

CREATE_KEY = (EventTypes.Create, "")
POWER_KEY = (EventTypes.PowerLevels, "")
GUEST_ACCESS_KEY = (EventTypes.GuestAccess, "")
JOIN_RULES_KEY = (EventTypes.JoinRules, "")


@parameterized_class(("room_version",), [("10",), ("12",)])
@patch(
    "famedly_control_synapse.client.FamedlyControlClient.get_group_members",
    new_callable=AsyncMock,
)
class TestManagedRoomCreation(ModuleApiTestCase):
    room_version: str

    def room_config(self):
        config = CreateManagedRoomRequest(
            room_alias_name="test_room_alias",
            name="Test Room",
            room_version=self.room_version,
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

    def _get_creator_powerlevel(self) -> int:
        """
        Per the room version, what is our defined room creator power level
        """
        if KNOWN_ROOM_VERSIONS[self.room_version].msc4289_creator_power_enabled:
            # Room's v12 and up have a power level that is not representable in
            # canonicaljson, 2**53, which is 9007199254740992
            return CREATOR_POWER_LEVEL
        # Rooms prior to version 12 are limited to (2**53) - 1, which is representable
        # and therefore can be in the JSON structure
        return CREATOR_POWER_LEVEL - 1

    def _get_state_map_of_room(self, room_id: str) -> StateMap:
        return self.get_success(
            self.hs.get_storage_controllers().state.get_current_state(
                room_id,
                StateFilter.from_types(
                    [
                        POWER_KEY,
                        CREATE_KEY,
                        GUEST_ACCESS_KEY,
                        JOIN_RULES_KEY,
                    ]
                ),
            )
        )

    def test_room_creation_success(self, mock_get_group_members) -> None:
        """Tests that managed room creation returns the expected response"""
        mock_get_group_members.return_value = [
            self.invitee
        ]  # in real case this should be external_ids
        channel = self.make_request(
            method="POST",
            path=self.CREATE_PATH,
            content=self.room_config(),
            access_token=self.creator_access_token,
            shorthand=False,
        )

        assert channel.code == HTTPStatus.OK, channel.result
        assert "room_id" in channel.json_body, "Response should contain room_id"
        room_id = channel.json_body["room_id"]

        # Check if the room has the correct configuration
        state_map = self._get_state_map_of_room(room_id)

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

    def test_room_creation_requires_admin(self, mock_get_group_members) -> None:
        """Only the configured Famedly Control admin may create rooms."""
        self.register_user("create_non_admin", "password", admin=False)
        non_admin_token = self.login("create_non_admin", "password")
        channel = self.make_request(
            method="POST",
            path=self.CREATE_PATH,
            content=self.room_config(),
            access_token=non_admin_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.FORBIDDEN, channel.result

    def test_room_creation_invalid_body(self, mock_get_group_members) -> None:
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

    def test_room_creation_with_no_fields(self, mock_get_group_members) -> None:
        """No fields are required: an empty request body creates a managed room
        with no alias, no name, no groups assigned, and the server's default room
        version."""
        channel = self.make_request(
            method="POST",
            path=self.CREATE_PATH,
            content={},
            access_token=self.creator_access_token,
            shorthand=False,
        )

        assert channel.code == HTTPStatus.OK, channel.result
        assert "room_id" in channel.json_body, channel.result
        room_id = channel.json_body["room_id"]
        assert channel.json_body["groups"] == []

        # No groups means no members were looked up.
        mock_get_group_members.assert_not_called()

        # The room is still a managed room, with an empty group list persisted.
        account_data = self.get_success(
            self.store.get_account_data_for_room(self.creator, room_id)
        )
        assert account_data == {MANAGED_ROOM_TYPE: {"groups": []}}

    def test_room_creation_with_partial_fields(self, mock_get_group_members) -> None:
        """A request providing only some optional fields (here just the name) is
        accepted rather than rejected for missing fields."""
        channel = self.make_request(
            method="POST",
            path=self.CREATE_PATH,
            content={"name": "Test Room"},
            access_token=self.creator_access_token,
            shorthand=False,
        )

        assert channel.code == HTTPStatus.OK, channel.result
        assert "room_id" in channel.json_body, channel.result
        assert channel.json_body["groups"] == []

    def test_default_room_powerlevels(self, mock_get_group_members) -> None:
        """Test that a room's default powerlevels are as expected"""
        mock_get_group_members.return_value = [
            self.invitee
        ]  # in real case this should be external_ids
        room_config = self.room_config()
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
        state_map = self._get_state_map_of_room(room_id)

        creator_pl = event_auth.get_user_power_level(self.creator, state_map)
        assert creator_pl == self._get_creator_powerlevel()

        # Check the invited user's power level
        invitee_pl = event_auth.get_user_power_level(self.invitee, state_map)
        assert invitee_pl == 0, "Invitee should have power level 0"

    def test_user_powerlevel_override_not_destroyed(
        self, mock_get_group_members
    ) -> None:
        """Test that a user powerlevel override is honored by room creation request"""
        mock_get_group_members.return_value = [
            self.invitee
        ]  # in real case this should be external_ids
        room_config = self.room_config()
        power_level_content_override = room_config.setdefault(
            "power_level_content_override", {}
        )
        users = power_level_content_override.setdefault("users", {})
        users.setdefault(self.invitee, 1)
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
        state_map = self._get_state_map_of_room(room_id)

        creator_pl = event_auth.get_user_power_level(self.creator, state_map)
        assert creator_pl == self._get_creator_powerlevel()

        # Check the invited user's power level
        invitee_pl = event_auth.get_user_power_level(self.invitee, state_map)
        assert invitee_pl == 1, "Invitee should have power level 1"

    def test_user_powerlevel_override_at_critical_level_forbidden(
        self, mock_get_group_members
    ) -> None:
        """Test that a user powerlevel override is not honored when set too high"""
        mock_get_group_members.return_value = [
            self.invitee
        ]  # in real case this should be external_ids
        room_config = self.room_config()
        power_level_content_override = room_config.setdefault(
            "power_level_content_override", {}
        )
        users = power_level_content_override.setdefault("users", {})
        users.setdefault(self.invitee, CREATOR_POWER_LEVEL - 1)
        channel = self.make_request(
            method="POST",
            path=self.CREATE_PATH,
            content=room_config,
            access_token=self.creator_access_token,
            shorthand=False,
        )
        # Should error with a 400 Bad Request
        assert channel.code == HTTPStatus.BAD_REQUEST, channel.result

    @parameterized.expand([("invite",), ("ban",), ("kick",)])
    def test_membership_action_powerlevel_cannot_be_circumvented(
        self, mock_get_group_members, membership_action: str
    ) -> None:
        """Test that membership action powerlevels can not be overridden"""
        mock_get_group_members.return_value = [
            self.invitee
        ]  # in real case this should be external_ids

        room_config = self.room_config()
        assert "power_level_content_override" in room_config
        # Adjust the specific action level we want to test. This is not supposed to be
        # allowed to change
        room_config["power_level_content_override"][membership_action] = 10

        channel = self.make_request(
            method="POST",
            path=self.CREATE_PATH,
            content=room_config,
            access_token=self.creator_access_token,
            shorthand=False,
        )
        # Should error with 400 Bad Request
        assert channel.code == HTTPStatus.BAD_REQUEST, channel.result

    @parameterized.expand(
        [(EventTypes.PowerLevels,), (EventTypes.JoinRules,), (EventTypes.GuestAccess,)]
    )
    def test_event_type_powerlevel_cannot_be_circumvented(
        self, mock_get_group_members, event_type: str
    ) -> None:
        """Test that event type powerlevels can not be overridden"""
        mock_get_group_members.return_value = [
            self.invitee
        ]  # in real case this should be external_ids

        room_config = self.room_config()
        assert "power_level_content_override" in room_config
        # Adjust the specific action level we want to test. This is not supposed to be
        # allowed to change
        assert "events" in room_config["power_level_content_override"]
        room_config["power_level_content_override"]["events"][event_type] = 10

        channel = self.make_request(
            method="POST",
            path=self.CREATE_PATH,
            content=room_config,
            access_token=self.creator_access_token,
            shorthand=False,
        )
        # Should be a 400 Bad Request
        assert channel.code == HTTPStatus.BAD_REQUEST, channel.result

    def test_room_created_with_members_joined(self, mock_get_group_members) -> None:
        """Tests that the users of the groups are joined to the room after creation"""
        test_group = "test_group_1"
        test_member_1 = self.register_user("test_member_1", "password")
        test_member_2 = self.register_user("test_member_2", "password")
        group_members = [test_member_1, test_member_2]
        mock_get_group_members.return_value = (
            group_members  # in real case this should be external_ids
        )

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

    @override_config({"run_background_tasks_on": "made_worker_name"})
    def test_non_background_worker_managed_room_creation_endpoint_disabled(
        self, mock_get_group_members
    ) -> None:
        """
        Tests that the managed room creation endpoint does not function on the incorrect
        Synapse worker
        """
        mock_get_group_members.return_value = [
            self.invitee
        ]  # in real case this should be external_ids
        channel = self.make_request(
            method="POST",
            path=self.CREATE_PATH,
            content=self.room_config(),
            access_token=self.creator_access_token,
            shorthand=False,
        )

        assert channel.code == HTTPStatus.NOT_FOUND, channel.result

    def test_room_creation_skipped_when_group_fetch_fails(
        self, mock_get_group_members
    ) -> None:
        """If fetching the group members fails (e.g. the Famedly Control API is
        unreachable), no room is created and an error is returned to the client."""
        mock_get_group_members.side_effect = FamedlyControlError(
            HTTPStatus.INTERNAL_SERVER_ERROR, "Connection refused"
        )

        with patch(
            "synapse.module_api.ModuleApi.create_room", new_callable=AsyncMock
        ) as mock_create_room:
            channel = self.make_request(
                method="POST",
                path=self.CREATE_PATH,
                content=self.room_config(),
                access_token=self.creator_access_token,
                shorthand=False,
            )

        assert channel.code == HTTPStatus.INTERNAL_SERVER_ERROR, channel.result
        assert "error" in channel.json_body
        # The room must never have been created in the first place.
        mock_create_room.assert_not_called()
        # The groups are echoed back so the client knows which request failed.
        assert channel.json_body.get("groups") == ["test_group"]

    def test_partial_room_deleted_when_assign_fails(
        self, mock_get_group_members
    ) -> None:
        """If assigning groups fails after the room has been created, the room is
        deleted immediately so we never leave behind a partial managed room.

        This asserts the room is genuinely gone: the shutdown kicks every local
        member, including the room creator who would otherwise hold the room open,
        and the room is purged from the database.

        `assign_groups_to_room` may also have queued retry-queue entries for the
        room before failing, and the background processor may have persisted a
        snapshot containing them. The rollback must drop the room from both the
        in-memory queue and the on-disk snapshot, otherwise the deleted room
        poisons retry-queue processing after a restart."""
        mock_get_group_members.return_value = [self.invitee]

        room_handler = self.hs.room_control.room_handler  # type: ignore[attr-defined]
        captured: dict[str, object] = {}

        async def queue_then_fail(
            room_id: str,
            admin_user_id: str,
            *args: object,
            **kwargs: object,
        ) -> None:
            # Record the room id and prove the creator is actually joined at this
            # point, so the "room is gone" assertions afterwards are meaningful.
            captured["room_id"] = room_id
            captured["members_before"] = (
                await room_handler.api._store.get_users_in_room(room_id)
            )
            # Simulate assign_groups_to_room queuing an unresolved member and the
            # snapshot being persisted (e.g. by the background retry processor)
            # before the failure surfaces.
            async with room_handler.retry_queue_lock:
                room_handler.retry_queue.add_external_id_to_room_queue(
                    room_id,
                    "@missing:test",
                    ActionReason(
                        reason="External User ID mapping not Found",
                        is_removal=False,
                        retry_count=0,
                        first_attempt_utc_ms=0,
                        latest_attempt_utc_ms=0,
                    ),
                )
                await room_handler.save_retry_queue_snapshot(admin_user_id)
            raise FamedlyControlError(HTTPStatus.INTERNAL_SERVER_ERROR, "boom")

        with patch.object(
            room_handler,
            "assign_groups_to_room",
            new=AsyncMock(side_effect=queue_then_fail),
        ):
            channel = self.make_request(
                method="POST",
                path=self.CREATE_PATH,
                content=self.room_config(),
                access_token=self.creator_access_token,
                shorthand=False,
            )

        assert channel.code == HTTPStatus.INTERNAL_SERVER_ERROR, channel.result

        room_id = captured["room_id"]
        assert isinstance(room_id, str) and room_id.startswith("!"), room_id
        members_before = captured["members_before"]
        assert isinstance(members_before, list)
        assert self.creator in members_before, members_before

        # delete_room only schedules a background shutdown-and-purge; let it run.
        self.reactor.advance(0.5)

        # The room must now be empty: the shutdown kicked every local member,
        # including the room creator.
        members_after = self.get_success(self.store.get_users_in_room(room_id))
        assert members_after == [], members_after
        # And the room itself must actually be gone
        assert self.get_success(self.store.get_room(room_id)) is None

        # The deleted room must be gone from the in-memory retry queue...
        assert room_id not in room_handler.retry_queue.rooms
        # ...and from the persisted snapshot
        persisted_queue = self.get_success(
            room_handler.get_retry_queue_snapshot(self.creator)
        )
        assert room_id not in persisted_queue.rooms

    def room_config_custom(
        self,
        creation_content: JsonDict | None = None,
        initial_state: list[JsonDict] | None = None,
    ) -> JsonDict:
        config_model = CreateManagedRoomRequest(
            room_alias_name="test_room_alias",
            name="Test Room",
            room_version=self.room_version,
            topic="This is a test room",
            groups=["test_group"],
        )
        config = config_model.model_dump()

        creation_content_dumped = CreationContent().model_dump()
        if creation_content:
            creation_content_dumped.update(**creation_content)

        if initial_state:
            config["initial_state"] = initial_state

        config["creation_content"] = creation_content_dumped
        return config

    def test_room_creation_fails_with_invalid_initial_state(
        self, mock_get_group_members
    ) -> None:
        """
        Test edge cases involving `initial_state` included in the room creation request.
        Specifically do not use the Pydantic models for this, as the model will not pass
        validation during construction. This is to test the handling of that failed
        validation by the endpoint.
        """

        custom_room_config = self.room_config_custom(
            initial_state=[
                {
                    "type": EventTypes.JoinRules,
                    "state_key": "",
                    "content": {"join_rule": Membership.JOIN},
                },
            ],
        )

        channel = self.make_request(
            method="POST",
            path=self.CREATE_PATH,
            content=custom_room_config,
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.BAD_REQUEST, channel.result
        assert (
            "Invalid request body" in channel.json_body["error"]
        ), "Response should contain an error message"

        custom_room_config = self.room_config_custom(
            initial_state=[
                {
                    "type": EventTypes.GuestAccess,
                    "state_key": "",
                    "content": {"guest_access": GuestAccess.CAN_JOIN},
                },
            ],
        )

        channel = self.make_request(
            method="POST",
            path=self.CREATE_PATH,
            content=custom_room_config,
            access_token=self.creator_access_token,
            shorthand=False,
        )

        assert channel.code == HTTPStatus.BAD_REQUEST, channel.result
        assert (
            "Invalid request body" in channel.json_body["error"]
        ), "Response should contain an error message"

        custom_room_config = self.room_config_custom(
            initial_state=[
                {
                    "type": EventTypes.PowerLevels,
                    "state_key": "",
                    "content": {"invite": 10},
                },
            ],
        )

        channel = self.make_request(
            method="POST",
            path=self.CREATE_PATH,
            content=custom_room_config,
            access_token=self.creator_access_token,
            shorthand=False,
        )

        # Passing in a power level event to initial_state with invalid content should
        # not be allowed
        assert channel.code == HTTPStatus.BAD_REQUEST, channel.result


@patch(
    "famedly_control_synapse.client.FamedlyControlClient.get_group_members",
    new_callable=AsyncMock,
)
class TestAssignGroupsToManagedRoom(ModuleApiTestCase):
    def assert_join_for_users(self, room_id: str, users: list[str]) -> None:
        """Helper function to assert the list of users are joined to the room."""
        for member in users:
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

    def assert_leave_for_users(self, room_id: str, users: list[str]) -> None:
        """Helper function to assert the users have left the room."""
        for member in users:
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

    def test_assign_groups_requires_admin(self, mock_get_group_members) -> None:
        """Only the configured Famedly Control admin may assign groups."""
        mock_get_group_members.return_value = []
        room_id = self._create_managed_room(name="Room", groups=[])
        self.register_user("assign_non_admin", "password", admin=False)
        non_admin_token = self.login("assign_non_admin", "password")
        channel = self.make_request(
            method="POST",
            path=self.BASE_PATH + f"/{room_id}/groups",
            content={"groups": ["test_group"]},
            access_token=non_admin_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.FORBIDDEN, channel.result

    def test_update_single_group_to_single_group(self, mock_get_group_members) -> None:
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
        self.assert_join_for_users(room_id, new_group_members)

        # Check if the old member who is not in the new group is removed from the room
        self.assert_leave_for_users(room_id, [test_member_1])

        # Check the account data is updated
        room_account_data = self.get_success(
            self.store.get_account_data_for_room(self.creator, room_id)
        )
        assert room_account_data == {
            MANAGED_ROOM_TYPE: {"groups": [test_new_group]}
        }, room_account_data

    def test_update_single_group_to_multiple_groups(
        self, mock_get_group_members
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
        self.assert_join_for_users(room_id, list(member_should_be_in_room))
        self.assert_leave_for_users(room_id, member_should_not_be_in_room)

        # Check the account data is updated
        room_account_data = self.get_success(
            self.store.get_account_data_for_room(self.creator, room_id)
        )
        assert room_account_data == {
            MANAGED_ROOM_TYPE: {"groups": new_group_info}
        }, room_account_data

    def test_update_multiple_groups_to_multiple_groups(
        self, mock_get_group_members
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
        self.assert_join_for_users(room_id, member_should_be_in_room)
        self.assert_leave_for_users(room_id, member_should_not_be_in_room)

        # Check the account data is updated
        room_account_data = self.get_success(
            self.store.get_account_data_for_room(self.creator, room_id)
        )
        assert room_account_data == {
            MANAGED_ROOM_TYPE: {"groups": new_group_info}
        }, room_account_data

    def test_user_from_removed_group_is_still_in_other_assigned_group(
        self, mock_get_group_members
    ) -> None:
        """
        Test the edge case when the member_r was both in group 1 and group 2, and
        group 2 was removed from the room.
        Old groups info: test_group_1, test_group_2
        New groups info: test_group_1
        """
        test_group_1 = "test_group_1"
        test_member_a = self.register_user("test_member_a", "password")
        test_member_r = self.register_user("test_member_r", "password")

        test_group_2 = "test_group_2"
        test_member_b = self.register_user("test_member_b", "password")

        old_group_info = [test_group_1, test_group_2]
        new_group_info = [test_group_1]

        def get_group_members(group_id):
            if group_id == test_group_1:
                return [test_member_a, test_member_r]
            elif group_id == test_group_2:
                return [test_member_b, test_member_r]
            return []

        mock_get_group_members.side_effect = get_group_members

        room_id = self._create_managed_room(name="Test Room", groups=old_group_info)
        self._test_get_membership(
            room_id,
            [test_member_a, test_member_b, test_member_r],
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

        member_should_be_in_room = [test_member_a, test_member_r]
        member_should_not_be_in_room = [test_member_b]

        self.assert_join_for_users(room_id, member_should_be_in_room)
        self.assert_leave_for_users(room_id, member_should_not_be_in_room)

    def test_prevent_room_creator_membership_change(
        self, mock_get_group_members
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

    def test_client_error_response(self, mock_get_group_members) -> None:
        """Test that if client raises an error, the endpoint returns an error with an
        error message."""
        # Create a managed room
        test_group = "test_group"
        test_member = self.register_user("test_member", "password")
        mock_get_group_members.return_value = [test_member]
        room_id = self._create_managed_room(name="Test Room", groups=[test_group])

        # Try to update groups with the client raising an error
        mock_get_group_members.side_effect = FamedlyControlError(
            HTTPStatus.INTERNAL_SERVER_ERROR, "Service unavailable"
        )
        channel = self.make_request(
            method="POST",
            path=self.BASE_PATH + f"/{room_id}/groups",
            content={"groups": ["new_group"]},
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == 500, channel.result
        assert "error" in channel.json_body

    def test_conversion_error_responds_with_success(
        self, mock_get_group_members
    ) -> None:
        """Test that if the mapping of external user id to local Synapse user doesn't
        exist and raises an error, the endpoint does not return an error"""
        # Create a managed room
        test_group = "test_group"
        test_member = self.register_user("test_member", "password")
        mock_get_group_members.return_value = [test_member]
        room_id = self._create_managed_room(name="Test Room", groups=[test_group])

        # Try to update groups and
        # batch_convert_external_user_ids_to_matrix_user_ids will return not_founds. The
        # call to the external api for getting the members of group "new_group" need to
        # not exist in the local database, which raises the error
        mock_get_group_members.return_value = ["external_id_1", "external_id_2"]
        channel = self.make_request(
            method="POST",
            path=self.BASE_PATH + f"/{room_id}/groups",
            content={"groups": ["new_group"]},
            access_token=self.creator_access_token,
            shorthand=False,
        )

        assert channel.code == HTTPStatus.OK, channel.result
        assert "room_id" in channel.json_body
        assert room_id == channel.json_body["room_id"]

    def test_leave_error_all_fail_response(
        self,
        mock_get_group_members,
    ) -> None:
        """Test that if leaving room raises errors for different users with different
        error codes, the endpoint returns success and metrics are incremented for
        each error code.
        """
        # Get the metric initial values for both error codes
        server_name = self.hs.hostname
        initial_value_unknown = famedly_control_user_sync_error.labels(
            error_code="M_UNKNOWN", server_name=server_name
        )._value.get()
        initial_value_unauthorized = famedly_control_user_sync_error.labels(
            error_code="M_UNAUTHORIZED", server_name=server_name
        )._value.get()

        # Create a managed room with test_group_1
        test_group_1 = "test_group_1"
        test_member_1 = self.register_user("test_member_1", "password")
        test_member_2 = self.register_user("test_member_2", "password")
        mock_get_group_members.return_value = [test_member_1, test_member_2]
        room_id = self._create_managed_room(name="Test Room", groups=[test_group_1])

        # Prepare test_group_2 which removes all 2 members and add a new member
        test_group_2 = "test_group_2"
        test_member_3 = self.register_user("test_member_3", "password")
        mock_get_group_members.side_effect = lambda group_id: (
            [test_member_1, test_member_2]
            if group_id == test_group_1
            else [test_member_3]
        )

        # Try to update groups with update_room_membership raising errors for different users
        def update_room_membership_side_effect(
            sender, target, room_id, new_membership, **kwargs
        ):
            if target == test_member_1:
                raise SynapseError(
                    HTTPStatus.INTERNAL_SERVER_ERROR, "Unexpected error", Codes.UNKNOWN
                )
            elif target == test_member_2:
                raise SynapseError(
                    HTTPStatus.UNAUTHORIZED, "Permission denied", Codes.UNAUTHORIZED
                )
            return None

        with patch(
            "synapse.module_api.ModuleApi.update_room_membership",
            new_callable=AsyncMock,
        ) as mock_update:
            mock_update.side_effect = update_room_membership_side_effect

            channel = self.make_request(
                method="POST",
                path=self.BASE_PATH + f"/{room_id}/groups",
                content={"groups": [test_group_2]},
                access_token=self.creator_access_token,
                shorthand=False,
            )
            # The response should show a success
            assert channel.code == HTTPStatus.OK, channel.result
            assert "room_id" in channel.json_body
            assert room_id == channel.json_body["room_id"]

        # Since both members failed to be removed, they should still be in the room
        # and the new member should be joined to the room
        self.assert_join_for_users(
            room_id, [test_member_1, test_member_2, test_member_3]
        )

        # Check both metrics are incremented by 1 each
        new_value_unknown = famedly_control_user_sync_error.labels(
            error_code="M_UNKNOWN", server_name=server_name
        )._value.get()
        new_value_unauthorized = famedly_control_user_sync_error.labels(
            error_code="M_UNAUTHORIZED", server_name=server_name
        )._value.get()
        assert new_value_unknown == initial_value_unknown + 1
        assert new_value_unauthorized == initial_value_unauthorized + 1

    def test_join_error_responds_with_success(
        self,
        mock_get_group_members,
    ) -> None:
        """Test that if joining room raises an error, the endpoint returns a success and
        error metric is updated."""
        # Get the metric initial value
        server_name = self.hs.hostname
        initial_value = famedly_control_user_sync_error.labels(
            error_code="M_NOT_FOUND", server_name=server_name
        )._value.get()

        # Create a managed room with test_group_1 which has 2 valid members.
        test_group_1 = "test_group_1"
        test_group_2 = "test_group_2"
        test_member_1 = self.register_user("test_member_1", "password")
        test_member_2 = self.register_user("test_member_2", "password")
        non_existent_user = f"@random_user:{server_name}"
        # Go ahead and register the external user id, this makes
        # `batch_convert_external_user_ids_to_matrix_user_ids()` happy so we can check
        # the response from `force_join_users_to_room()`. How realistic this is: unknown
        self.register_external_id(non_existent_user)
        mock_get_group_members.side_effect = lambda group_id: (
            [test_member_1, test_member_2]
            if group_id == test_group_1
            else [test_member_2, non_existent_user]
        )
        room_id = self._create_managed_room(name="Test Room", groups=[test_group_1])

        # Now update the group assignment to test_group_2, which has an already-joined
        # member and one non-existent member. `force_join_users_to_room` will skip for
        # the already-joined member and return error record for the non-existent member.
        def update_room_membership_side_effect(
            sender, target, room_id, new_membership, **kwargs
        ):
            if target == non_existent_user:
                raise SynapseError(404, "User not found", Codes.NOT_FOUND)
            return None

        with patch(
            "synapse.module_api.ModuleApi.update_room_membership",
            new_callable=AsyncMock,
        ) as mock_update:
            mock_update.side_effect = update_room_membership_side_effect

            channel = self.make_request(
                method="POST",
                path=self.BASE_PATH + f"/{room_id}/groups",
                content={"groups": [test_group_2]},
                access_token=self.creator_access_token,
                shorthand=False,
            )
            assert channel.code == HTTPStatus.OK, channel.result
            assert "room_id" in channel.json_body
            assert room_id == channel.json_body["room_id"]

        # Check the metric value is increased by 1
        new_value = famedly_control_user_sync_error.labels(
            error_code="M_NOT_FOUND", server_name=server_name
        )._value.get()
        assert new_value == initial_value + 1

    def test_both_join_and_leave_errors_respond_with_success(
        self,
        mock_get_group_members,
    ) -> None:
        """Test that if both joining and leaving room raise errors, the endpoint returns
        a success."""
        # Create a managed room with test_group_1
        test_group_1 = "test_group_1"
        test_group_2 = "test_group_2"
        test_member_1 = self.register_user("test_member_1", "password")
        test_member_2 = self.register_user("test_member_2", "password")
        non_existent_user = f"@random_user:{self.hs.hostname}"
        # Go ahead and register the external user id, this makes
        # `batch_convert_external_user_ids_to_matrix_user_ids()` happy so we can check
        # the response from `force_join_users_to_room()`. How realistic this is: unknown
        self.register_external_id(non_existent_user)
        mock_get_group_members.side_effect = lambda group_id: (
            [test_member_1, test_member_2]
            if group_id == test_group_1
            else [non_existent_user]
        )
        room_id = self._create_managed_room(name="Test Room", groups=[test_group_1])

        # Now update the group assignment to test_group_2, which only has non-existent member.
        # Both `force_join_users_to_room` and `remove_users_from_room` will return errors.
        with (
            patch(
                "famedly_control_synapse.room_handler.ManagedRoomHandler.remove_users_from_room",
                new_callable=AsyncMock,
            ) as mock_remove,
        ):
            mock_remove.return_value = {test_member_1: "Unexpected error"}

            channel = self.make_request(
                method="POST",
                path=self.BASE_PATH + f"/{room_id}/groups",
                content={"groups": [test_group_2]},
                access_token=self.creator_access_token,
                shorthand=False,
            )
            assert channel.code == HTTPStatus.OK, channel.result
            assert "room_id" in channel.json_body
            assert room_id == channel.json_body["room_id"]

    @override_config({"run_background_tasks_on": "fake_worker"})
    def test_non_background_worker_assign_groups_endpoint_disabled(
        self, mock_get_group_members
    ) -> None:
        """
        Tests that the assign groups endpoint does not function on the incorrect
        Synapse worker
        """
        channel = self.make_request(
            method="POST",
            path=self.BASE_PATH + "/!fake_room_does_not_matter/groups",
            content={"groups": ["test_new_group"]},
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.NOT_FOUND, channel.result


@patch(
    "famedly_control_synapse.client.FamedlyControlClient.get_group_members",
    new_callable=AsyncMock,
)
class TestListManagedRooms(ModuleApiTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer):
        super().prepare(reactor, clock, homeserver)
        self.non_admin = self.register_user("non_admin", "password", admin=False)
        self.non_admin_token = self.login("non_admin", "password")
        self.account_data_handler = homeserver.get_account_data_handler()

    def test_list_requires_admin(self, mock_get_group_members) -> None:
        """Non-admin users should get a 403."""
        channel = self.make_request(
            method="GET",
            path=self.LIST_PATH,
            access_token=self.non_admin_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.FORBIDDEN, channel.result

    def test_list_requires_auth(self, mock_get_group_members) -> None:
        """Unauthenticated requests should get a 401."""
        channel = self.make_request(
            method="GET",
            path=self.LIST_PATH,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.UNAUTHORIZED, channel.result

    def test_list_empty(self, mock_get_group_members) -> None:
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

    def test_list_returns_managed_rooms(self, mock_get_group_members) -> None:
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

    def test_list_pagination(self, mock_get_group_members) -> None:
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

    @override_config({"run_background_tasks_on": "fake_worker"})
    def test_non_background_worker_list_endpoint_disabled(
        self, mock_get_group_members
    ) -> None:
        """
        Tests that the assign groups endpoint does not function on the incorrect
        Synapse worker
        """
        channel = self.make_request(
            method="GET",
            path=self.LIST_PATH,
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.NOT_FOUND, channel.result

    def test_list_search_term_by_name(self, mock_get_group_members) -> None:
        """search_term matches rooms by name substring."""
        self._create_managed_room(name="Alpha Room")
        self._create_managed_room(name="Beta Room")
        self._create_managed_room(name="Gamma Room")
        self._create_managed_room(name="#special Room")

        channel = self.make_request(
            method="GET",
            path=f"{self.LIST_PATH}?search_term=Beta",
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        assert channel.json_body["total_room_count_estimate"] == 1
        assert len(channel.json_body["chunk"]) == 1
        assert channel.json_body["chunk"][0]["name"] == "Beta Room"

        # Names starting with '#' are matched literally, not treated as alias sigil
        channel = self.make_request(
            method="GET",
            path=f"{self.LIST_PATH}?search_term=%23special",
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        assert channel.json_body["total_room_count_estimate"] == 1
        assert channel.json_body["chunk"][0]["name"] == "#special Room"

    def test_list_search_term_case_insensitive(self, mock_get_group_members) -> None:
        """search_term is case-insensitive."""
        self._create_managed_room(name="Alpha Room")
        self._create_managed_room(name="Beta Room")

        channel = self.make_request(
            method="GET",
            path=f"{self.LIST_PATH}?search_term=alpha",
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        assert channel.json_body["total_room_count_estimate"] == 1
        assert channel.json_body["chunk"][0]["name"] == "Alpha Room"

    def test_list_search_term_by_alias(self, mock_get_group_members) -> None:
        """search_term matches rooms by canonical alias.

        _create_managed_room sets room_alias_name=test_room_{N}, so the canonical
        alias becomes #test_room_{N}:testserver.com.
        """
        # Capture the counter before creating rooms so we can predict the alias assigned.
        start = self._room_counter
        self._create_managed_room(name="First")
        n = start + 1
        alias_localpart = f"test_room_{n}"
        full_alias = f"#{alias_localpart}:{self.server_name_for_this_server}"

        self._create_managed_room(name="Second")
        self._create_managed_room(name="Third")

        # All of these should uniquely identify "First"
        for search_term in [
            alias_localpart,  # bare localpart: test_room_N
            f"#{alias_localpart}",  # with sigil, no domain: #test_room_N
            full_alias,  # full canonical alias: #test_room_N:testserver.com
        ]:
            channel = self.make_request(
                method="GET",
                path=f"{self.LIST_PATH}?search_term={search_term}",
                access_token=self.creator_access_token,
                shorthand=False,
            )
            assert channel.code == HTTPStatus.OK, channel.result
            assert channel.json_body["total_room_count_estimate"] == 1, search_term
            assert channel.json_body["chunk"][0]["name"] == "First", search_term

        # Domain-only matches all three rooms since they all share the same server
        channel = self.make_request(
            method="GET",
            path=f"{self.LIST_PATH}?search_term={self.server_name_for_this_server}",
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        assert channel.json_body["total_room_count_estimate"] == 3

    def test_list_search_term_by_room_id(self, mock_get_group_members) -> None:
        """search_term matches rooms by exact room_id."""
        room_id = self._create_managed_room(name="Findable Room")
        self._create_managed_room(name="Other Room")

        channel = self.make_request(
            method="GET",
            path=f"{self.LIST_PATH}?search_term={room_id}",
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        assert channel.json_body["total_room_count_estimate"] == 1
        assert channel.json_body["chunk"][0]["room_id"] == room_id

    def test_list_search_term_no_match(self, mock_get_group_members) -> None:
        """search_term with no matching rooms returns an empty chunk."""
        self._create_managed_room(name="Alpha Room")

        channel = self.make_request(
            method="GET",
            path=f"{self.LIST_PATH}?search_term=nomatch",
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        assert channel.json_body["total_room_count_estimate"] == 0
        assert channel.json_body["chunk"] == []

    def test_list_filter_by_group_id(self, mock_get_group_members) -> None:
        """managed_room_group_id returns only rooms that belong to that group."""
        mock_get_group_members.return_value = []
        self._create_managed_room(name="Group A Room", groups=["group_a"])
        self._create_managed_room(name="Group B Room", groups=["group_b"])
        self._create_managed_room(
            name="Both Groups Room", groups=["group_a", "group_b"]
        )

        channel = self.make_request(
            method="GET",
            path=f"{self.LIST_PATH}?managed_room_group_id=group_a",
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        assert channel.json_body["total_room_count_estimate"] == 2
        names = {r["name"] for r in channel.json_body["chunk"]}
        assert names == {"Group A Room", "Both Groups Room"}

    def test_list_filter_by_group_id_no_match(self, mock_get_group_members) -> None:
        """managed_room_group_id with no matching group returns empty chunk."""
        mock_get_group_members.return_value = []
        self._create_managed_room(name="Some Room", groups=["group_a"])

        channel = self.make_request(
            method="GET",
            path=f"{self.LIST_PATH}?managed_room_group_id=nonexistent_group",
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        assert channel.json_body["total_room_count_estimate"] == 0
        assert channel.json_body["chunk"] == []

    def test_list_order_by_name_asc(self, mock_get_group_members) -> None:
        """order_by=name with dir=f returns rooms sorted by name ascending."""
        self._create_managed_room(name="Zebra Room")
        self._create_managed_room(name="Apple Room")
        self._create_managed_room(name="Mango Room")

        channel = self.make_request(
            method="GET",
            path=f"{self.LIST_PATH}?order_by=name&dir=f",
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        names = [r["name"] for r in channel.json_body["chunk"]]
        assert names == sorted(names)

    def test_list_order_by_name_desc(self, mock_get_group_members) -> None:
        """order_by=name with dir=b returns rooms sorted by name descending."""
        self._create_managed_room(name="Zebra Room")
        self._create_managed_room(name="Apple Room")
        self._create_managed_room(name="Mango Room")

        channel = self.make_request(
            method="GET",
            path=f"{self.LIST_PATH}?order_by=name&dir=b",
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        names = [r["name"] for r in channel.json_body["chunk"]]
        assert names == sorted(names, reverse=True)

    def test_list_invalid_order_by(self, mock_get_group_members) -> None:
        """Deprecated or unknown order_by values return 400."""
        for bad_value in ("alphabetical", "size", "unknown_field"):
            channel = self.make_request(
                method="GET",
                path=f"{self.LIST_PATH}?order_by={bad_value}",
                access_token=self.creator_access_token,
                shorthand=False,
            )
            assert (
                channel.code == HTTPStatus.BAD_REQUEST
            ), f"Expected 400 for order_by={bad_value!r}, got {channel.code}"

    def test_list_invalid_dir(self, mock_get_group_members) -> None:
        """Invalid dir values return 400."""
        channel = self.make_request(
            method="GET",
            path=f"{self.LIST_PATH}?dir=x",
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.BAD_REQUEST, channel.result

    def test_list_total_count_reflects_filter(self, mock_get_group_members) -> None:
        """total_room_count_estimate reflects the filtered result count, not total."""
        self._create_managed_room(name="Match Room A")
        self._create_managed_room(name="Match Room B")
        self._create_managed_room(name="No Match")

        channel = self.make_request(
            method="GET",
            path=f"{self.LIST_PATH}?search_term=Match+Room",
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        assert channel.json_body["total_room_count_estimate"] == 2
        assert len(channel.json_body["chunk"]) == 2

    def test_list_pagination_with_search_term(self, mock_get_group_members) -> None:
        """from and limit work correctly when search_term is active."""
        for i in range(4):
            self._create_managed_room(name=f"Filter Room {i}")
        self._create_managed_room(name="Excluded")

        # First page
        channel = self.make_request(
            method="GET",
            path=f"{self.LIST_PATH}?search_term=Filter+Room&limit=2&order_by=name",
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        assert channel.json_body["total_room_count_estimate"] == 4
        assert len(channel.json_body["chunk"]) == 2
        assert "next_batch" in channel.json_body
        assert "prev_batch" not in channel.json_body

        # Second page
        next_batch = channel.json_body["next_batch"]
        channel = self.make_request(
            method="GET",
            path=f"{self.LIST_PATH}?search_term=Filter+Room&limit=2&order_by=name&from={next_batch}",
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        assert len(channel.json_body["chunk"]) == 2
        assert "next_batch" not in channel.json_body
        assert "prev_batch" in channel.json_body

    def test_list_combined_filters(self, mock_get_group_members) -> None:
        """search_term, managed_room_group_id, and order_by work in combination."""
        mock_get_group_members.return_value = []
        self._create_managed_room(name="Project Alpha", groups=["team_a"])
        self._create_managed_room(name="Project Beta", groups=["team_a"])
        self._create_managed_room(name="Project Gamma", groups=["team_b"])
        self._create_managed_room(name="Other Room", groups=["team_a"])

        channel = self.make_request(
            method="GET",
            path=f"{self.LIST_PATH}?search_term=Project&managed_room_group_id=team_a&order_by=name&dir=b",
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        assert channel.json_body["total_room_count_estimate"] == 2
        names = [r["name"] for r in channel.json_body["chunk"]]
        assert names == ["Project Beta", "Project Alpha"]


@patch(
    "famedly_control_synapse.client.FamedlyControlClient.get_group_members",
    new_callable=AsyncMock,
)
class TestGetManagedRoom(ModuleApiTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer):
        super().prepare(reactor, clock, homeserver)
        self.non_admin = self.register_user("non_admin", "password", admin=False)
        self.non_admin_token = self.login("non_admin", "password")

    def _get_path(self, room_id: str) -> str:
        return f"{self.BASE_PATH}/{room_id}"

    def test_get_requires_admin(self, mock_get_group_members) -> None:
        """Non-admin users should get a 403."""
        mock_get_group_members.return_value = []
        room_id = self._create_managed_room(name="Room", groups=[])

        channel = self.make_request(
            method="GET",
            path=self._get_path(room_id),
            access_token=self.non_admin_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.FORBIDDEN, channel.result

    def test_get_requires_auth(self, mock_get_group_members) -> None:
        """Unauthenticated requests should get a 401."""
        mock_get_group_members.return_value = []
        room_id = self._create_managed_room(name="Room", groups=[])

        channel = self.make_request(
            method="GET",
            path=self._get_path(room_id),
            shorthand=False,
        )
        assert channel.code == HTTPStatus.UNAUTHORIZED, channel.result

    def test_get_returns_managed_room(self, mock_get_group_members) -> None:
        """A managed room should be returned as a ManagedRoomChunk."""
        user_1 = self.register_user("user1", "password")
        user_2 = self.register_user("user2", "password")

        def get_members_by_group(group_id):
            if group_id == "group1":
                return [user_1]
            if group_id == "group2":
                return [user_2]
            return []

        mock_get_group_members.side_effect = get_members_by_group

        room_id = self._create_managed_room(
            name="Fetched Room", groups=["group1", "group2"]
        )

        channel = self.make_request(
            method="GET",
            path=self._get_path(room_id),
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        body = channel.json_body
        assert body["room_id"] == room_id
        assert body["name"] == "Fetched Room"
        assert body[MANAGED_ROOM_TYPE]["groups"] == ["group1", "group2"]

    def test_get_unknown_room_returns_404(self, mock_get_group_members) -> None:
        """A room that is not managed returns 404, whether or not it exists."""
        # A well-formed room ID that does not exist.
        channel = self.make_request(
            method="GET",
            path=self._get_path(f"!doesnotexist:{self.server_name_for_this_server}"),
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.NOT_FOUND, channel.result

        # A real room that exists but is not managed.
        mock_get_group_members.return_value = []
        unmanaged_room_id = self.helper.create_room_as(
            self.creator, tok=self.creator_access_token
        )
        channel = self.make_request(
            method="GET",
            path=self._get_path(unmanaged_room_id),
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.NOT_FOUND, channel.result

    def test_list_route_not_shadowed(self, mock_get_group_members) -> None:
        """The '/{roomId}' route must not swallow the literal '/rooms' route."""
        mock_get_group_members.return_value = []
        self._create_managed_room(name="Room", groups=[])

        channel = self.make_request(
            method="GET",
            path=self.LIST_PATH,
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        # The list endpoint returns a paginated chunk, not a single ManagedRoomChunk.
        assert "chunk" in channel.json_body
