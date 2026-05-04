from http import HTTPStatus
from unittest.mock import AsyncMock, patch

import parameterized
from synapse.api.constants import EventTypes
from synapse.module_api.errors import Codes, SynapseError
from synapse.server import HomeServer
from synapse.types.state import StateFilter
from synapse.util.clock import Clock
from synapse.util.duration import Duration
from twisted.internet.testing import MemoryReactor

from famedly_control_synapse.client import ManyGroupsDiffResponse
from famedly_control_synapse.rest.types import CreateManagedRoomRequest
from famedly_control_synapse.room_handler import (
    ManagedRoomHandler,
    famedly_control_user_sync_error,
    parse_missing_items,
)
from famedly_control_synapse.sync import GroupMembershipSyncer
from famedly_control_synapse.types import ManagedRoomRetryQueue
from tests.utils.module_api_testcase import ModuleApiTestCase


@parameterized.parameterized_class(("room_version",), [("10",), ("12",)])
class TestRoomHandler(ModuleApiTestCase):
    room_version: str

    def prepare(self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer):
        super().prepare(reactor, clock, homeserver)
        self.room_handler = self.hs.room_control.room_handler
        self.server_name = self.hs.hostname

    def _check_users_joined_to_room(
        self,
        room: str,
        members: list[str],
    ) -> None:
        """Helper method to check if the users have joined a room."""
        for member in members:
            path = "/rooms/%s/state/m.room.member/%s" % (room, member)
            channel = self.make_request(
                "GET", path, access_token=self.creator_access_token
            )
            assert channel.code == HTTPStatus.OK, channel.result
            assert channel.json_body["membership"] == "join", channel.result

    def _create_managed_room_without_members(
        self,
    ) -> str:
        config = CreateManagedRoomRequest(
            room_alias_name="membership_test_room",
            name="Membership Test Room",
            groups=["test_group"],
            room_version=self.room_version,
        )
        with (
            patch(
                "famedly_control_synapse.client.FamedlyControlClient.get_group_members",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            channel = self.make_request(
                method="POST",
                path=self.CREATE_PATH,
                content=config.model_dump(),
                access_token=self.creator_access_token,
                shorthand=False,
            )
        assert channel.code == HTTPStatus.OK, channel.result
        return channel.json_body["room_id"]

    def test_force_join_users_to_room(self) -> None:
        """Test that users can be force joined to a room."""
        user_a = self.register_user("test_user_a", "password")
        user_b = self.register_user("test_user_b", "password")
        user_c = self.register_user("test_user_c", "password")
        room_id = self._create_managed_room_without_members()

        error = self.get_success(
            self.room_handler.force_join_users_to_room(
                room_id=room_id,
                user_mxids=[user_a, user_b, user_c],
                admin_user_id=self.creator,
            )
        )
        assert error == {}, f"Expected no errors, but got: {error}"
        self._check_users_joined_to_room(room_id, [user_a, user_b, user_c])

    def test_force_join_users_already_in_room(self) -> None:
        """Test that force joining users who are already in the room is handled correctly."""
        user_a = self.register_user("test_user_a", "password")
        user_b = self.register_user("test_user_b", "password")
        room_id = self._create_managed_room_without_members()

        # Force join the same user twice
        error = self.get_success(
            self.room_handler.force_join_users_to_room(
                room_id=room_id, user_mxids=[user_a, user_b], admin_user_id=self.creator
            )
        )
        assert error == {}, f"Expected no errors, but got: {error}"
        self._check_users_joined_to_room(room_id, [user_a, user_b])

        error = self.get_success(
            self.room_handler.force_join_users_to_room(
                room_id=room_id, user_mxids=[user_a, user_b], admin_user_id=self.creator
            )
        )
        assert error == {}, f"Expected no errors, but got: {error}"
        self._check_users_joined_to_room(room_id, [user_a, user_b])

    def test_force_join_users_including_failed_users(self) -> None:
        """Test that force joining users handles failures in a mixed scenario."""
        # Get the metric initial value
        initial_value = famedly_control_user_sync_error.labels(
            error_code="M_BAD_STATE", server_name=self.server_name
        )._value.get()

        user_a = self.register_user("test_user_a", "password")
        user_b = self.register_user("test_user_b", "password")
        room_id = self._create_managed_room_without_members()

        # Ban user_b to make force join fail for this user
        self.helper.ban(
            room_id, self.creator, user_b, HTTPStatus.OK, self.creator_access_token
        )

        # Attempt to force join a mix of users where one will fail (banned user)
        error = self.get_success(
            self.room_handler.force_join_users_to_room(
                room_id=room_id, user_mxids=[user_a, user_b], admin_user_id=self.creator
            )
        )
        # Should return an error dict with user_b (the banned user)
        assert error, "Expected errors, but got nothing"
        assert user_b in error, f"Expected error for {user_b}, but got: {error}"
        # Check that the valid user (user_a) successfully joined the room
        self._check_users_joined_to_room(room_id, [user_a])

        # Check the metric value is increased by 1
        new_value = famedly_control_user_sync_error.labels(
            error_code="M_BAD_STATE", server_name=self.server_name
        )._value.get()
        assert new_value == initial_value + 1

    def test_force_join_users_with_nonexistent_user(self) -> None:
        """Test that force_join_users_to_room catches error for non-existent user."""
        # Get the metric initial value
        initial_value = famedly_control_user_sync_error.labels(
            error_code="M_NOT_FOUND", server_name=self.server_name
        )._value.get()

        user_a = self.register_user("test_user_a", "password")
        non_existent_user = f"@random_user:{self.server_name}"
        room_id = self._create_managed_room_without_members()

        error = self.get_success(
            self.room_handler.force_join_users_to_room(
                room_id=room_id,
                user_mxids=[non_existent_user, user_a],
                admin_user_id=self.creator,
            )
        )
        # Should return an error dict with the non-existent user
        assert error, "Expected errors, but got nothing"
        assert (
            non_existent_user in error
        ), f"Expected error for {non_existent_user}, but got: {error}"
        assert error[non_existent_user] == "User does not exist on this server"

        # Check that the valid user (user_a) successfully joined the room
        self._check_users_joined_to_room(room_id, [user_a])

        # Check the metric value is increased by 1
        new_value = famedly_control_user_sync_error.labels(
            error_code="M_NOT_FOUND", server_name=self.server_name
        )._value.get()
        assert new_value == initial_value + 1

    def test_remove_users_from_room(self) -> None:
        """Test that users can be removed from a room."""
        user_a = self.register_user("test_user_a", "password")
        user_b = self.register_user("test_user_b", "password")
        room_id = self._create_managed_room_without_members()

        # Add the users to the room to make sure they can be removed
        error = self.get_success(
            self.room_handler.force_join_users_to_room(
                room_id=room_id, user_mxids=[user_a, user_b], admin_user_id=self.creator
            )
        )
        assert error == {}, f"Expected no errors, but got: {error}"
        self._check_users_joined_to_room(room_id, [user_a, user_b])

        # Now remove the users from the room
        error = self.get_success(
            self.room_handler.remove_users_from_room(
                creator_id=self.creator, user_mxids=[user_a, user_b], room_id=room_id
            )
        )
        assert error == {}, f"Expected no errors, but got: {error}"
        # Check that the users have been removed
        for member in [user_a, user_b]:
            path = "/rooms/%s/state/m.room.member/%s" % (room_id, member)
            channel = self.make_request(
                "GET", path, access_token=self.creator_access_token
            )
            assert channel.code == HTTPStatus.OK, channel.result
            assert channel.json_body["membership"] == "leave", channel.result

    def test_remove_users_from_room_with_nonexistent_user(self) -> None:
        """Test that remove_users_from_room catches user non-existent case."""
        # Get the metric initial value
        initial_value = famedly_control_user_sync_error.labels(
            error_code="M_NOT_FOUND", server_name=self.server_name
        )._value.get()

        user_a = self.register_user("test_user_a", "password")
        non_existent_user = f"@nonexistent:{self.server_name}"
        room_id = self._create_managed_room_without_members()

        # Add the valid user to the room
        error = self.get_success(
            self.room_handler.force_join_users_to_room(
                room_id=room_id, user_mxids=[user_a], admin_user_id=self.creator
            )
        )
        assert error == {}, f"Expected no errors, but got: {error}"
        self._check_users_joined_to_room(room_id, [user_a])

        # Now attempt to remove both the valid user and the non-existent user
        error = self.get_success(
            self.room_handler.remove_users_from_room(
                creator_id=self.creator,
                user_mxids=[user_a, non_existent_user],
                room_id=room_id,
            )
        )
        # Should return an error dict with the non-existent user
        assert error, "Expected errors, but got nothing"
        assert (
            non_existent_user in error
        ), f"Expected error for {non_existent_user}, but got: {error}"
        # Check that the valid user (user_a) has been removed from the room
        path = "/rooms/%s/state/m.room.member/%s" % (room_id, user_a)
        channel = self.make_request("GET", path, access_token=self.creator_access_token)
        assert channel.code == HTTPStatus.OK, channel.result
        assert channel.json_body["membership"] == "leave", channel.result

        # Check the metric value is increased by 1
        new_value = famedly_control_user_sync_error.labels(
            error_code="M_NOT_FOUND", server_name=self.server_name
        )._value.get()
        assert new_value == initial_value + 1

    def test_remove_users_from_room_with_not_a_member_user(self) -> None:
        """Test that remove_users_from_room skips the user is not a member case."""
        user_a = self.register_user("test_user_a", "password")
        non_member_user = self.register_user("non_member_user", "password")
        room_id = self._create_managed_room_without_members()

        # Add a valid user to the room
        error = self.get_success(
            self.room_handler.force_join_users_to_room(
                room_id=room_id, user_mxids=[user_a], admin_user_id=self.creator
            )
        )
        assert error == {}, f"Expected no errors, but got: {error}"
        self._check_users_joined_to_room(room_id, [user_a])

        # Now attempt to remove both the valid user and the non-member user
        error = self.get_success(
            self.room_handler.remove_users_from_room(
                creator_id=self.creator,
                user_mxids=[user_a, non_member_user],
                room_id=room_id,
            )
        )
        # This does not return an error because the method should skip the non-member user and still remove the valid user
        assert error == {}
        # Check that the valid user (user_a) has been removed from the room
        path = "/rooms/%s/state/m.room.member/%s" % (room_id, user_a)
        channel = self.make_request("GET", path, access_token=self.creator_access_token)
        assert channel.code == HTTPStatus.OK, channel.result
        assert channel.json_body["membership"] == "leave", channel.result

    def test_remove_users_unexpected_error(self) -> None:
        # Get the metric initial value
        initial_value = famedly_control_user_sync_error.labels(
            error_code="M_UNKNOWN", server_name=self.server_name
        )._value.get()

        user_a = self.register_user("test_user_a", "password")
        room_id = self._create_managed_room_without_members()

        # Add the valid user to the room
        error = self.get_success(
            self.room_handler.force_join_users_to_room(
                room_id=room_id, user_mxids=[user_a], admin_user_id=self.creator
            )
        )
        assert error == {}, f"Expected no errors, but got: {error}"
        self._check_users_joined_to_room(room_id, [user_a])

        # Now attempt to remove the valid user and simulate an unexpected error
        with patch.object(
            self.room_handler.api, "update_room_membership", new_callable=AsyncMock
        ) as mock_update:
            mock_update.side_effect = SynapseError(
                500, "Unexpected error", Codes.UNKNOWN
            )

            error = self.get_success(
                self.room_handler.remove_users_from_room(
                    creator_id=self.creator,
                    user_mxids=[user_a],
                    room_id=room_id,
                )
            )
            assert error, "Expected errors, but got nothing"
            assert user_a in error, f"Expected error for {user_a}, but got: {error}"

        # Check that the the user has not been removed from the room due to the unexpected error
        path = "/rooms/%s/state/m.room.member/%s" % (room_id, user_a)
        channel = self.make_request("GET", path, access_token=self.creator_access_token)
        assert channel.code == HTTPStatus.OK, channel.result
        assert channel.json_body["membership"] == "join", channel.result

        # Check the metric value is increased by 1
        new_value = famedly_control_user_sync_error.labels(
            error_code="M_UNKNOWN", server_name=self.server_name
        )._value.get()
        assert new_value == initial_value + 1

    def test_batch_convert_external_user_ids_to_matrix_user_ids(self) -> None:
        """Test that batch conversion of external user IDs to Matrix user IDs works correctly."""
        external_user_ids = []
        matrix_user_ids = []

        # Generate 105 pairs of matrix_user_id and external_user_id
        for i in range(105):
            external_user_id = f"external_user_{i}"
            external_user_ids.append(external_user_id)
            matrix_user_id = f"@matrix_user_{i}:{self.server_name_for_this_server}"
            matrix_user_ids.append(matrix_user_id)

        # Insert into the user_external_ids table
        self.get_success(
            self.store.db_pool.simple_insert_many(
                table="user_external_ids",
                keys=["user_id", "external_id", "auth_provider"],
                values=[
                    (matrix_user_id, external_id, "https://idp.example.com/")
                    for matrix_user_id, external_id in zip(
                        matrix_user_ids, external_user_ids
                    )
                ],
                desc="test_insert_multiple_user_external_ids",
            )
        )

        # Test the batch conversion method
        result_mapping = self.get_success(
            self.room_handler.batch_convert_external_user_ids_to_matrix_user_ids(
                external_user_ids
            )
        )
        assert set(result_mapping.keys()) == set(external_user_ids)
        assert set(result_mapping.values()) == set(matrix_user_ids)
        not_found = parse_missing_items(result_mapping.keys(), external_user_ids)
        assert not_found == []
        assert len(result_mapping) == 105

    def test_batch_convert_with_missing_users(self) -> None:
        """Test that batch conversion handles missing users gracefully.

        This can happen when users exist in Zitadel but haven't logged into Synapse yet.
        The method should return partial results instead of raising an exception.
        """
        # Mix of existing and non-existing users
        external_user_ids = [
            "existing_user_1",
            "missing_user_1",  # This user is in Zitadel but not in Synapse
            "existing_user_2",
            "missing_user_2",  # This user is in Zitadel but not in Synapse
        ]

        # Only insert the "existing" users
        self.get_success(
            self.store.db_pool.simple_insert_many(
                table="user_external_ids",
                keys=["user_id", "external_id", "auth_provider"],
                values=[
                    (
                        f"@existing_1:{self.server_name_for_this_server}",
                        "existing_user_1",
                        "https://idp.example.com/",
                    ),
                    (
                        f"@existing_2:{self.server_name_for_this_server}",
                        "existing_user_2",
                        "https://idp.example.com/",
                    ),
                ],
                desc="test_insert_partial_user_external_ids",
            )
        )

        result_mapping = self.get_success(
            self.room_handler.batch_convert_external_user_ids_to_matrix_user_ids(
                external_user_ids
            )
        )
        not_founds = parse_missing_items(result_mapping.keys(), external_user_ids)
        assert len(result_mapping) == 2
        assert len(not_founds) == 2
        assert (
            f"@existing_1:{self.server_name_for_this_server}" in result_mapping.values()
        )
        assert (
            f"@existing_2:{self.server_name_for_this_server}" in result_mapping.values()
        )
        assert "missing_user_1" in not_founds
        assert "missing_user_2" in not_founds

    @patch(
        "famedly_control_synapse.client.FamedlyControlClient.get_group_members",
        new_callable=AsyncMock,
    )
    def test_get_room_creator(self, mock_get_group_members) -> None:
        # Create a room and get its ID
        group_1 = "group_1"
        member_a = self.register_user("test_member_a", "password")
        member_b = self.register_user("test_member_b", "password")
        group_1_members = [member_a, member_b]

        mock_get_group_members.return_value = (
            group_1_members  # in real case this should be external_ids
        )

        room_id = self._create_managed_room(
            name="Test Room with Group Members", groups=[group_1]
        )
        # Get the room creator using the method
        creator = self.get_success(self.room_handler.get_room_creator(room_id))

        # Assert that the creator is correct
        assert creator == self.creator


def assert_room_queue_values(
    main_queue_model: ManagedRoomRetryQueue,
    label: str,
    room_id: str | None = None,
    not_room_id: str | None = None,
    in_external_ids: str | list[str] | None = None,
    not_in_external_ids: str | list[str] | None = None,
    in_member_ids: str | list[str] | None = None,
    not_in_member_ids: str | list[str] | None = None,
) -> None:
    """
    Assert for each value in the ManagedRoomRetryQueue, including the room_id
    """
    if not_room_id:
        assert (
            not_room_id not in main_queue_model.rooms
        ), f"{label}, {not_room_id} found IN queue: {main_queue_model}"
        # Nothing else to do with this, if the room is not supposed to be there we can
        # not check anything else
        return

    assert (
        room_id is not None
    ), "have to include at least one of 'room_id' or 'not_room_id'"
    assert (
        room_id in main_queue_model.rooms
    ), f"{label}, {room_id} NOT found in queue: {main_queue_model}"

    room_retry_queue = main_queue_model.rooms[room_id]

    external_id_queue = room_retry_queue.external_ids
    # First check for external ids that should be in the queue
    if isinstance(in_external_ids, list):
        for item in in_external_ids:
            assert (
                item in external_id_queue
            ), f"{label}, {item} should be in external_ids: {external_id_queue}"
    elif isinstance(in_external_ids, str):
        assert (
            in_external_ids in external_id_queue
        ), f"{label}, {in_external_ids} should be in external_ids: {external_id_queue}"

    # Check for external ids that should NOT be in the queue
    if isinstance(not_in_external_ids, list):
        for item in not_in_external_ids:
            assert (
                item not in external_id_queue
            ), f"{label}, {item} should NOT be in external_ids: {external_id_queue}"
    elif isinstance(not_in_external_ids, str):
        assert (
            not_in_external_ids not in external_id_queue
        ), f"{label}, {not_in_external_ids} should NOT be in external_ids: {external_id_queue}"

    members_queue = room_retry_queue.members
    # First check for mxids that should be in the members queue
    if isinstance(in_member_ids, list):
        for item in in_member_ids:
            assert (
                item in members_queue
            ), f"{label}, {item} should be in members: {members_queue}"
    elif isinstance(in_member_ids, str):
        assert (
            in_member_ids in members_queue
        ), f"{label}, {in_member_ids} should be in members: {members_queue}"

    # Check for mxids that should NOT be in the members queue
    if isinstance(not_in_member_ids, list):
        for item in not_in_member_ids:
            assert (
                item not in members_queue
            ), f"{label}, {item} should NOT be in members: {members_queue}"
    elif isinstance(not_in_member_ids, str):
        assert (
            not_in_member_ids not in members_queue
        ), f"{label}, {not_in_member_ids} should NOT be in members: {members_queue}"


class RetryQueueTestCase(ModuleApiTestCase):
    """
    Test the retry queue for adding and removing members from rooms.

    Infrastructure to automatically handle groups in a simulated way similar to the
    external api has been introduced

    Easy-mode instructions:
    Create your user with the normal register_user() function, and that user will have
        an associated external_user_id record inserted.
    If you should need an external_user_id record but not an actual user, call
        register_external_id()
    Creating or updating a group is easy with add_or_update_mock_group(). This will
        insert the necessary data structures to support the sync system and any
        functions that would call the external api to determine what member should be in
        a group
    If utilizing the sync loop, call wait_for_sync_loop() and it will return once the
        next iteration of the sync loop has occurred that advanced the sync token. The
        long poll of the sync loop may be adjusted with self._long_poll_duration_seconds

    A Note on tests that may appear missing: There is no way to save an external user id
        into the database and then try and register the user(without editing the
        database to remove the external user id). This will trigger a SQL constraint
        error, as an already persisted external user id can not be written twice

    """

    groups: dict[str, list[str]]
    group_diff: dict[int, dict[str, list[dict[str, str]]]]
    """mapping of sync_token(as int) -> mapping of group_id -> list of mappings to external_user_id and action"""

    def setUp(self) -> None:
        # Reset these for each test. Strictly speaking this is not necessary
        self.groups = {}
        self.group_diff = {}
        self.group_member_patcher = patch(
            "famedly_control_synapse.client.FamedlyControlClient.get_group_members",
            new=AsyncMock(side_effect=self._get_group_members),
        )
        self.mock_get_group_diff = AsyncMock(side_effect=self._get_group_diff)
        self.group_diff_patcher = patch(
            "famedly_control_synapse.client.FamedlyControlClient.get_all_groups_diffs",
            new=self.mock_get_group_diff,
        )

        # Used as part of `_get_group_diff()` function to simulate a long poll. Set it
        # less than `sync_polling_interval_seconds` setting to verify a loop is waiting
        # this time and not for the forced wait. The default config for this test case
        # is 5 seconds, so set this to less than that
        self._long_poll_duration_seconds = 3

        self.group_member_patcher.start()
        self.group_diff_patcher.start()
        super().setUp()

    def tearDown(self) -> None:
        self.group_member_patcher.stop()
        self.group_diff_patcher.stop()
        super().tearDown()

    def default_config(self):
        conf = super().default_config()
        conf["modules"][0]["config"]["sync_enabled"] = True
        conf["modules"][0]["config"]["sync_polling_interval_seconds"] = 5
        conf["modules"][0]["config"]["error_retry_queue_enabled"] = True
        conf["modules"][0]["config"]["error_retry_queue_interval_seconds"] = 7
        return conf

    def prepare(self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer):
        super().prepare(reactor, clock, homeserver)
        self.syncer: GroupMembershipSyncer = self.hs.room_control.syncer  # type: ignore[attr-defined]
        self.room_handler: ManagedRoomHandler = self.hs.room_control.room_handler  # type: ignore[attr-defined]

        # Let's give ourselves a seed room so the sync loop has the appropriate
        # structures in place. Will need a member and a group, then create the room.
        # These will not be used directly in the following tests
        self.member_1 = self.register_user("seed_room_member_ignore", "password")
        self.add_or_update_mock_group("seed_group", [self.member_1])
        self.seed_room = self._create_managed_room(groups=["seed_group"])

        # Will need a basic member for each room in the following tests. We will add
        # the members group_id to each room as part of it's creation
        self.base_room_member = self.register_user("base-room_member", "password")
        self.base_member_group_id = "Base member group"
        self.add_or_update_mock_group(
            self.base_member_group_id, [self.base_room_member]
        )

    def _get_group_members(self, group: str) -> list[str]:
        """Part of the mock infrastructure for the external /get_group_members call"""
        # We have largely decided that a group can not be used on this endpoint unless
        # it actually exists, as the reference we would otherwise use to know of its
        # existence would have come from the same source. Until otherwise stated, this
        # is an error on our end.
        assert (
            group in self.groups
        ), f"{group} not in established groups. Remember to use `add_or_update_mock_group()` to add it"
        return self.groups.get(group, [])

    def add_or_update_mock_group(
        self, group_name: str, new_group_members: list[str]
    ) -> None:
        """
        Part of the mock infrastructure for the external /get_group_members call. Use
        this before you add any groups to any rooms to add groups to the simulated
        external api
        """
        # Save this to calculate differences after wards
        last_group_members = self.groups.get(group_name, [])
        # set the actual group
        self.groups[group_name] = new_group_members
        # prepare the difference calculations to establish who left and who joined
        self._add_to_group_diff(group_name, last_group_members, new_group_members)

    async def _get_group_diff(
        self, sync: str | None, timeout: int
    ) -> ManyGroupsDiffResponse:
        """
        Part of the mock infrastructure for the external /get_all_groups_diffs call.

        Ensure if you want a long poll to wait for a given amount of time that you set
        the _long_poll_duration_seconds attribute to a value more than 0.
        Otherwise, you will only bump the reactor and not actually long-poll
        """
        # sync is supposed to represent the *next* batch to watch for. While this
        # implies that the value is guaranteed to exist in a future iteration, this may
        # not be the case.
        # sync_token can be None, in which case we assume the last/largest sync_token is
        # returned(this behavior is not documented)

        if sync is None:
            sync_int = self._get_largest_current_sync_token()
        else:
            sync_int = int(sync)

        if sync_int not in self.group_diff and timeout > 0:
            # If the sync token that was requested for does not exist yet,
            # artificially wait here to simulate a long poll
            assert (
                self._long_poll_duration_seconds <= timeout
            ), "Check duration for long poll"
            await self.module_api._clock.sleep(
                Duration(seconds=self._long_poll_duration_seconds)
            )

        # This works because if we waited and the data is still not there, we return
        # nothing(an empty object) and the same token as before. If we did not have to
        # wait, then it is likely that is because the sync token was not passed in to
        # begin with, which should represent "give me last update"(which I don't think
        # is actually correct. Shouldn't it just return what the last token was?)
        data = self.group_diff.get(sync_int, {})

        return ManyGroupsDiffResponse.model_validate(
            {"next_sync": str(sync_int if not data else sync_int + 1), "data": data}
        )

    def _get_largest_current_sync_token(self) -> int:
        """Calculate the largest sync token available based on what has been inserted"""
        sync_token_set_view = self.group_diff.keys()
        return max(sync_token_set_view, default=0)

    def _add_to_group_diff(
        self,
        group_name: str,
        previous_group_members: list[str],
        new_group_members: list[str],
    ) -> None:
        # First de-duplicate and use a structure which is easier to remove elements from
        new_group_member_set = set(new_group_members)
        last_group_members_set = set(previous_group_members)
        # Set up the differences of members
        members_added = new_group_member_set.difference(last_group_members_set)
        members_removed = last_group_members_set.difference(new_group_member_set)
        # Set up the lists as required for the response body
        members_removed_prepped = [
            {"user_id": user_id, "action": "Rem"} for user_id in members_removed
        ]
        members_added_prepped = [
            {"user_id": user_id, "action": "Add"} for user_id in members_added
        ]
        # Cram it into the appropriate structure
        mapping_of_changes = {
            group_name: members_removed_prepped + members_added_prepped
        }

        next_token = self._get_largest_current_sync_token() + 1
        self.group_diff[next_token] = mapping_of_changes

    def assign_groups_to_room(self, room_id: str, list_of_group_ids: list[str]) -> None:
        """
        Helper to make the api request that assigns groups to specific rooms
        """
        channel = self.make_request(
            method="POST",
            path=self.BASE_PATH + f"/{room_id}/groups",
            content={"groups": list_of_group_ids},
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result

    def _get_users_in_room(self, room_id: str) -> list[str]:
        state_map = self.get_success(
            self.hs.get_storage_controllers().state.get_current_state(
                room_id,
                StateFilter.from_types(
                    [
                        (EventTypes.Member, None),
                    ]
                ),
            )
        )
        list_of_members = [
            state_key
            for (_, state_key), event_base in state_map.items()
            if event_base.content["membership"] == "join"
        ]
        return list_of_members

    def assert_users_in_room(
        self, room_id: str, list_of_room_users_to_check: list[str]
    ) -> None:
        """Not all lists of users are in order. Make sure it is before checking"""
        list_of_existing_room_users = self._get_users_in_room(room_id)
        self.assertEqual(
            sorted(list_of_existing_room_users), sorted(list_of_room_users_to_check)
        )

    def wait_for_sync_loop(self, reset_mock_exception_after: int = 0) -> int:
        """
        Wait for the sync loop. Optionally reset any Exceptions mocked for the
        /get_all_group_diffs call and return the count of iterations to check
        expectations.

        A maximum number of iterations is enforced to avoid tests hanging indefinitely
        if the sync token never advances (for example, due to a regression or a
        misconfigured mock).
        """
        assert self.syncer._sync_token is not None
        current_sync_token = int(self.syncer._sync_token)
        count = 0
        # Borrow the polling interval seconds and apply a multiplier to guarantee that
        # no test can accidentally "run away" and never finish
        max_iterations = self.syncer.polling_interval_seconds * 10
        while True:
            count += 1

            self.reactor.advance(1.0)

            # Borrow that our pseudo-sync token is an int to track the monotonic
            if current_sync_token < int(self.syncer._sync_token):
                break

            # reset_mock_exception_after being its default of 0 means this won't be hit
            if reset_mock_exception_after == count:
                self.mock_get_group_diff.side_effect = self._get_group_diff

            if count >= max_iterations:
                raise AssertionError(
                    f"wait_for_sync_loop exceeded {max_iterations} iterations "
                    f"without the sync token advancing (initial={current_sync_token}, "
                    f"current={self.syncer._sync_token}). This likely indicates a "
                    f"regression in the sync loop or a misconfigured mock."
                )

        return count

    def test_nonexistent_external_id_in_queue_success_via_retry_loop(self) -> None:
        """
        Test that a non-existent external id can be placed into the retry queue and
        resolved via the retry loop once the user is created
        """
        # Recall we have our basic member that will be added to the room. All further
        # testing actions will use this next group being defined.
        group_name_2 = "test_group_resolved"
        # Default handling of creating a user will use the mxid as the external id. If
        # this gets registered into the database without a registered mxid to match it,
        # when we register the mxid it will loudly complain about that SQL constraint
        # violation. This is good enough for testing
        external_user_id = "@retry-loop-friend:test"
        self.add_or_update_mock_group(group_name_2, [external_user_id])

        # Create room. One user should be missing
        room_id = self._create_managed_room(
            groups=[self.base_member_group_id, group_name_2]
        )

        # Check the queue snapshot from the database. The missing user should be there
        managed_room_model = self.get_success(
            self.room_handler.get_retry_queue_snapshot(self.creator)
        )
        assert_room_queue_values(
            managed_room_model,
            "Before processing",
            room_id=room_id,
            in_external_ids=external_user_id,
            not_in_external_ids=self.base_room_member,
            # technically, the external_user_id should not ever be in the members queue.
            # But our external user ids are also mxids, so in the interest of verifying
            # we did not foobar something, let's make sure.
            not_in_member_ids=[self.base_room_member, external_user_id],
        )

        # Create missing user now
        user_2_mxid = self.register_user("retry-loop-friend", "password")
        self.login("retry-loop-friend", "password")

        # Bump the reactor so the retry queue processor has a chance to run
        self.reactor.advance(7.0)

        # Test that user is now in room
        self.assert_users_in_room(
            room_id, [self.creator, self.base_room_member, user_2_mxid]
        )

        # Test the in-memory queue does not have the room_id, which means the queue is
        # empty
        in_mem_managed_room_model = self.room_handler.retry_queue
        assert_room_queue_values(
            in_mem_managed_room_model,
            "(in-memory)After processing",
            not_room_id=room_id,
        )

        # and the saved snapshot of the queue
        managed_room_model = self.get_success(
            self.room_handler.get_retry_queue_snapshot(self.creator)
        )
        assert_room_queue_values(
            managed_room_model,
            "(snapshot)After processing",
            not_room_id=room_id,
        )

    def test_nonexistent_external_id_in_queue_success_via_sync_loop(self) -> None:
        """
        Test that a non-existent external id can be placed into the retry queue and
        resolved via the sync loop once the user is created
        """
        # Recall we have our basic member that will be added to the room. All further
        # testing actions will use this next group being defined.
        group_name_2 = "test_group_resolved"
        # Default handling of creating a user will use the mxid as the external id. If
        # this gets registered into the database without a registered mxid to match it,
        # when we register the mxid it will loudly complain about that SQL constraint
        # violation. This is good enough for testing
        external_user_id = "@sync-loop-friend:test"
        self.add_or_update_mock_group(group_name_2, [external_user_id])

        # Create room. One user should be missing
        room_id = self._create_managed_room(
            groups=[self.base_member_group_id, group_name_2]
        )

        # Check the queue snapshot from the database. The missing user should be there
        managed_room_model = self.get_success(
            self.room_handler.get_retry_queue_snapshot(self.creator)
        )
        assert_room_queue_values(
            managed_room_model,
            "Before processing",
            room_id=room_id,
            in_external_ids=external_user_id,
            not_in_external_ids=self.base_room_member,
            # technically, the external_user_id should not ever be in the members queue.
            # But our external user ids are also mxids, so in the interest of verifying
            # we did not foobar something, let's make sure.
            not_in_member_ids=[self.base_room_member, external_user_id],
        )

        # Create missing user now
        user_2_mxid = self.register_user("sync-loop-friend", "password")
        self.login("sync-loop-friend", "password")

        # Reconcile that a group was added to a room, this should trigger that the
        # previously missing users can be force joined and should remove the pending
        # entry in the error retry queue
        self.wait_for_sync_loop()

        # Test that user is now in room
        self.assert_users_in_room(
            room_id, [self.creator, self.base_room_member, user_2_mxid]
        )

        # Test the in-memory queue does not have the room_id, which means the queue is
        # empty
        in_mem_managed_room_model = self.room_handler.retry_queue
        assert_room_queue_values(
            in_mem_managed_room_model,
            "(in-memory)After processing",
            not_room_id=room_id,
        )

        # and the saved snapshot of the queue
        managed_room_model = self.get_success(
            self.room_handler.get_retry_queue_snapshot(self.creator)
        )
        assert_room_queue_values(
            managed_room_model,
            "(snapshot)After processing",
            not_room_id=room_id,
        )

    def test_nonexistent_external_id_in_queue_removed_by_sync_loop(
        self,
    ) -> None:
        """
        Test that a non-existent external user ID can be inserted into the retry queue
        and then removed when the group membership changes via the sync loop. The user
        should not be in the room nor in the queue
        """
        # Recall we have our basic member that will be added to the room. All further
        # testing actions will use this next group being defined.
        group_name_2 = "test_external_id_removed_sync_loop"
        # This user will not end up existing on the system, which effectively places
        # them onto the retry queue
        user_2 = "@disappearing-sync-loop-friend:test"
        self.add_or_update_mock_group(group_name_2, [user_2])

        # Create room. One user should be missing
        room_id = self._create_managed_room(
            groups=[self.base_member_group_id, group_name_2]
        )

        # Test the snapshot queue
        managed_room_model = self.get_success(
            self.room_handler.get_retry_queue_snapshot(self.creator)
        )
        assert_room_queue_values(
            managed_room_model,
            "(snapshot)Before Processing",
            room_id=room_id,
            in_external_ids=user_2,
            not_in_external_ids=self.base_room_member,
        )

        # Remove the user from the system, they are not going to exist after all
        self.add_or_update_mock_group(group_name_2, [])

        # This should update the retry queue and the no-op should occur
        self.wait_for_sync_loop()

        # Bump the reactor so the retry queue processor has a chance to run
        self.reactor.advance(7.0)

        # Test that user is not in room
        self.assert_users_in_room(room_id, [self.creator, self.base_room_member])

        # Test the in-memory queue does not have the room_id, which means the queue is
        # empty
        in_mem_managed_room_model = self.room_handler.retry_queue
        assert_room_queue_values(
            in_mem_managed_room_model,
            "(in-memory)After processing",
            not_room_id=room_id,
        )

        # and the saved snapshot of the queue
        managed_room_model = self.get_success(
            self.room_handler.get_retry_queue_snapshot(self.creator)
        )
        assert_room_queue_values(
            managed_room_model,
            "(snapshot)After processing",
            not_room_id=room_id,
        )

    def test_nonexistent_external_id_in_queue_removed_by_assignment_endpoint(
        self,
    ) -> None:
        """
        Test that a non-existent external user ID can be inserted into the retry queue
        and then removed when the group membership changes via the assignment endpoint.
        The user should not be in the room nor in the queue
        """
        # Recall we have our basic member that will be added to the room. All further
        # testing actions will use this next group being defined.
        group_name_2 = "test_external_id_removed_endpoint"
        # This user will not end up existing on the system, which effectively places
        # them onto the retry queue
        user_2 = "@disappearing-assignment-endpoint-friend:test"
        self.add_or_update_mock_group(group_name_2, [user_2])

        # Create room. One user should be missing
        room_id = self._create_managed_room(
            groups=[self.base_member_group_id, group_name_2]
        )

        # Test the snapshot queue
        managed_room_model = self.get_success(
            self.room_handler.get_retry_queue_snapshot(self.creator)
        )
        assert_room_queue_values(
            managed_room_model,
            "(snapshot)Before processing",
            room_id=room_id,
            in_external_ids=user_2,
            not_in_external_ids=self.base_room_member,
        )

        # Remove the user from the system, they are not going to exist after all
        self.add_or_update_mock_group(group_name_2, [])

        # Change the room assignment via the endpoint
        self.assign_groups_to_room(room_id, [self.base_member_group_id])

        # Bump the reactor so the retry queue processor has a chance to run
        self.reactor.advance(7.0)

        # Test that user is not in room
        self.assert_users_in_room(room_id, [self.creator, self.base_room_member])

        # Test the in-memory queue does not have the room_id, which means the queue is
        # empty
        in_mem_managed_room_model = self.room_handler.retry_queue
        assert_room_queue_values(
            in_mem_managed_room_model,
            "(in-memory)After processing",
            not_room_id=room_id,
        )

        # and the saved snapshot of the queue
        managed_room_model = self.get_success(
            self.room_handler.get_retry_queue_snapshot(self.creator)
        )
        assert_room_queue_values(
            managed_room_model,
            "(snapshot)After processing",
            not_room_id=room_id,
        )

    @parameterized.parameterized.expand([(False,), (True,)])
    def test_joined_user_deactivated_will_not_rejoin_room(
        self, erase_user: bool
    ) -> None:
        """
        Test that a user can be deactivated and the queue can not accidentally re-add
        them to the room
        """
        # The testing process for this:
        # 1. Have a room with two members, each in their own group. Run the sync loop
        #   to allow the group assignment to catch up
        # 2. Deactivate one member, ensure they are no longer in the room
        # 3. To verify they can not be added back to the room via the sync loop or the
        #   retry queue, add that deactivated member to a new group.
        # look for errors when leaving a group

        # Step 1

        # Recall we have our basic member that will be added to the room. All further
        # testing actions will use this next group being defined.
        group_name_2 = "test_deactivating_user_removed_step_1"
        # This user will be joined to the room, then deactivated.
        user_2 = self.register_user("deactivating-soon-sync-loop-friend", "password")
        self.add_or_update_mock_group(group_name_2, [user_2])

        # Run the sync loop. This allows the delay from constructing the group to
        # propagate to our system and advance the sync token
        self.wait_for_sync_loop()

        # Create room
        room_id = self._create_managed_room(
            groups=[self.base_member_group_id, group_name_2]
        )

        # Test the snapshot queue
        managed_room_model = self.get_success(
            self.room_handler.get_retry_queue_snapshot(self.creator)
        )
        assert_room_queue_values(
            managed_room_model, "(snapshot)Before deactivation", not_room_id=room_id
        )

        # Test that user_2 is in room
        self.assert_users_in_room(
            room_id, [self.creator, self.base_room_member, user_2]
        )

        # Step 2

        # Deactivate the user using the endpoint. This should be largely immediate
        channel = self.make_request(
            "POST",
            "/_synapse/admin/v1/deactivate/%s" % user_2,
            {"erase": erase_user},
            shorthand=False,
            access_token=self.creator_access_token,
        )
        assert channel.code == 200

        # Test that user is not in room
        self.assert_users_in_room(room_id, [self.creator, self.base_room_member])

        # Grab the leave membership event, as later we want to compare its timestamp.
        # This helps to prove that a user was not rejoined and then rekicked
        user_2_leave_event = self.get_success(
            self.storage_controllers.state.get_current_state_event(
                room_id, EventTypes.Member, user_2
            )
        )
        assert user_2_leave_event is not None
        assert user_2_leave_event.origin_server_ts > 0

        # Step 3

        # create a new group for this user to test that group can be added to the room
        # without the user also being added. Wait for the sync loop to catch up
        group_name_3 = "test_deactivating_user_removed_step_3"
        self.add_or_update_mock_group(group_name_3, [user_2])
        self.wait_for_sync_loop()

        # This will have an immediate attempt at joining
        self.assign_groups_to_room(room_id, [self.base_member_group_id, group_name_3])

        # Test that user is not in room
        self.assert_users_in_room(room_id, [self.creator, self.base_room_member])
        new_user_2_leave_event = self.get_success(
            self.storage_controllers.state.get_current_state_event(
                room_id, EventTypes.Member, user_2
            )
        )
        assert new_user_2_leave_event is not None
        assert (
            new_user_2_leave_event.origin_server_ts
            == user_2_leave_event.origin_server_ts
        )

        # Now, test the scenario again but let the sync loop take the load of trying to
        # assign the user
        group_name_4 = "starting-as-empty-group"
        self.add_or_update_mock_group(group_name_4, [])
        # maybe...?
        self.wait_for_sync_loop()
        self.assign_groups_to_room(room_id, [self.base_member_group_id, group_name_4])
        self.add_or_update_mock_group(group_name_4, [user_2])
        self.wait_for_sync_loop()
        # Test that user is not in room
        self.assert_users_in_room(room_id, [self.creator, self.base_room_member])
        new_user_2_leave_event = self.get_success(
            self.storage_controllers.state.get_current_state_event(
                room_id, EventTypes.Member, user_2
            )
        )
        assert new_user_2_leave_event is not None
        assert (
            new_user_2_leave_event.origin_server_ts
            == user_2_leave_event.origin_server_ts
        )

        # Test the in-memory queue is empty
        in_mem_managed_room_model = self.room_handler.retry_queue
        assert_room_queue_values(
            in_mem_managed_room_model,
            "(in-memory)After processing",
            not_room_id=room_id,
        )

        # ...and the snapshot queue is empty too
        managed_room_model = self.get_success(
            self.room_handler.get_retry_queue_snapshot(self.creator)
        )
        assert_room_queue_values(
            managed_room_model, "(in-memory)After processing", not_room_id=room_id
        )
