from http import HTTPStatus
from unittest.mock import AsyncMock, patch

from synapse.api.constants import EventTypes
from synapse.api.errors import HttpResponseException
from synapse.server import HomeServer
from synapse.types.state import StateFilter
from synapse.util.clock import Clock
from synapse.util.duration import Duration
from twisted.internet.testing import MemoryReactor

from famedly_control_synapse.client import (
    DiffRecord,
    ManyGroupsDiffResponse,
    MembershipAction,
)
from famedly_control_synapse.repository import ManagedRoomRepository
from famedly_control_synapse.rest.types import CreateManagedRoomRequest
from famedly_control_synapse.sync import GroupMembershipSyncer
from tests.utils.module_api_testcase import ModuleApiTestCase


class TestGroupMembershipSync(ModuleApiTestCase):
    def default_config(self):
        conf = super().default_config()
        conf["modules"][0]["config"]["sync_enabled"] = False
        return conf

    def prepare(self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer):
        super().prepare(reactor, clock, homeserver)
        self.syncer: GroupMembershipSyncer = self.hs.room_control.syncer  # type: ignore[attr-defined]
        self.repository: ManagedRoomRepository = self.hs.room_control.repository  # type: ignore[attr-defined]
        self.member_1 = self.register_user("sync_member_1", "password")
        self.member_2 = self.register_user("sync_member_2", "password")
        self.member_3 = self.register_user("sync_member_3", "password")

    def _create_managed_room_for_sync(
        self, name: str = "Sync Test Room", groups: list[str] | None = None
    ) -> str:
        groups = groups or ["sync_group"]
        with (
            patch(
                "famedly_control_synapse.client.FamedlyControlClient.get_group_members",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            config = CreateManagedRoomRequest(
                room_alias_name=f"sync_room_{self._room_counter + 1}",
                name=name,
                groups=groups,
                room_version=self.hs.config.server.default_room_version.identifier,
            )
            self._room_counter += 1
            channel = self.make_request(
                method="POST",
                path=self.CREATE_PATH,
                content=config.model_dump(),
                access_token=self.creator_access_token,
                shorthand=False,
            )
            assert channel.code == HTTPStatus.OK, channel.result
            # The endpoint for room creation establishes the sync token for us. To allow
            # the process_sync() function to work, just fill it in the same way it is
            # done by start_sync_loop().
            entry = self.get_success(self.repository.get_sync_token_entry())
            assert entry is not None
            self.syncer._sync_token_user_id, self.syncer._sync_token = entry
            assert self.syncer._sync_token_user_id == self.creator
            return channel.json_body["room_id"]

    def _get_membership(self, room_id: str, user_id: str) -> str | None:
        path = f"/_matrix/client/v3/rooms/{room_id}/state/m.room.member/{user_id}"
        channel = self.make_request("GET", path, access_token=self.creator_access_token)
        if channel.code == HTTPStatus.OK:
            return channel.json_body["membership"]
        return None

    @patch(
        "famedly_control_synapse.client.FamedlyControlClient.get_all_groups_diffs",
        new_callable=AsyncMock,
    )
    def test_sync_adds_users_to_room(self, mock_get_diffs) -> None:
        """Sync should add new users while leaving existing members unchanged."""
        room_id = self._create_managed_room_for_sync(groups=["group_a"])

        # First sync: add member_1 as an existing member
        mock_get_diffs.return_value = ManyGroupsDiffResponse(
            next_sync="1",
            data={
                "group_a": [
                    DiffRecord(user_id=self.member_1, action=MembershipAction.ADD),
                ],
            },
        )
        self.get_success(self.syncer._process_sync())
        assert self._get_membership(room_id, self.member_1) == "join"

        # Second sync: add member_2 and member_3
        mock_get_diffs.return_value = ManyGroupsDiffResponse(
            next_sync="2",
            data={
                "group_a": [
                    DiffRecord(user_id=self.member_2, action=MembershipAction.ADD),
                    DiffRecord(user_id=self.member_3, action=MembershipAction.ADD),
                ],
            },
        )
        self.get_success(self.syncer._process_sync())

        # New users joined
        assert self._get_membership(room_id, self.member_2) == "join"
        assert self._get_membership(room_id, self.member_3) == "join"
        # Existing member unchanged
        assert self._get_membership(room_id, self.member_1) == "join"

    @patch(
        "famedly_control_synapse.client.FamedlyControlClient.get_all_groups_diffs",
        new_callable=AsyncMock,
    )
    def test_sync_removes_users_from_room(self, mock_get_diffs) -> None:
        """Sync should remove specified users while leaving others unchanged."""
        room_id = self._create_managed_room_for_sync(groups=["group_b"])

        # First add all three members
        mock_get_diffs.return_value = ManyGroupsDiffResponse(
            next_sync="1",
            data={
                "group_b": [
                    DiffRecord(user_id=self.member_1, action=MembershipAction.ADD),
                    DiffRecord(user_id=self.member_2, action=MembershipAction.ADD),
                    DiffRecord(user_id=self.member_3, action=MembershipAction.ADD),
                ],
            },
        )
        self.get_success(self.syncer._process_sync())
        assert self._get_membership(room_id, self.member_1) == "join"
        assert self._get_membership(room_id, self.member_2) == "join"
        assert self._get_membership(room_id, self.member_3) == "join"

        # Now remove only member_1
        mock_get_diffs.return_value = ManyGroupsDiffResponse(
            next_sync="2",
            data={
                "group_b": [
                    DiffRecord(user_id=self.member_1, action=MembershipAction.REM),
                ],
            },
        )
        self.get_success(self.syncer._process_sync())

        # Removed user is gone
        assert self._get_membership(room_id, self.member_1) == "leave"
        # Remaining members unchanged
        assert self._get_membership(room_id, self.member_2) == "join"
        assert self._get_membership(room_id, self.member_3) == "join"

    @patch(
        "famedly_control_synapse.client.FamedlyControlClient.get_group_members",
        new_callable=AsyncMock,
    )
    @patch(
        "famedly_control_synapse.client.FamedlyControlClient.get_all_groups_diffs",
        new_callable=AsyncMock,
    )
    def test_sync_handles_removal_of_user_who_belongs_to_multiple_groups(
        self, mock_get_diffs, mock_get_group_members
    ) -> None:
        """
        Sync should not remove a user who was removed from a group but is still a member
        of another assigned group.

        e.g. a user was a member of test_group_1 and test_group_2. The user was removed from test_group_1
        but is still a member of test_group_2.
        groups info before sync: test_group_1, test_group_2
        groups info after sync: test_group_1, test_group_2
        """
        room_id = self._create_managed_room_for_sync(
            groups=["test_group_1", "test_group_2"]
        )

        # First add all members
        mock_get_diffs.return_value = ManyGroupsDiffResponse(
            next_sync="1",
            data={
                "test_group_1": [
                    DiffRecord(user_id=self.member_1, action=MembershipAction.ADD),
                    DiffRecord(user_id=self.member_3, action=MembershipAction.ADD),
                ],
                "test_group_2": [
                    DiffRecord(user_id=self.member_1, action=MembershipAction.ADD),
                    DiffRecord(user_id=self.member_2, action=MembershipAction.ADD),
                ],
            },
        )

        def mock_members_by_group(group_id: str) -> list[str]:
            members = {
                "test_group_1": [self.member_1, self.member_3],
                "test_group_2": [self.member_1, self.member_2],
            }
            return members.get(group_id, [])

        mock_get_group_members.side_effect = mock_members_by_group
        self.get_success(self.syncer._process_sync())
        assert self._get_membership(room_id, self.member_1) == "join"
        assert self._get_membership(room_id, self.member_2) == "join"
        assert self._get_membership(room_id, self.member_3) == "join"

        # Now remove member_1 from the test_group_1
        mock_get_diffs.return_value = ManyGroupsDiffResponse(
            next_sync="2",
            data={
                "test_group_1": [
                    DiffRecord(user_id=self.member_1, action=MembershipAction.REM),
                ],
            },
        )
        mock_get_group_members.side_effect = lambda group_id: {
            "test_group_1": [self.member_3],
            "test_group_2": [self.member_1, self.member_2],
        }.get(group_id, [])
        self.get_success(self.syncer._process_sync())

        # Removed user should still stay, since it is still a member of test_group_2
        assert self._get_membership(room_id, self.member_1) == "join"
        # Remaining members unchanged
        assert self._get_membership(room_id, self.member_2) == "join"
        assert self._get_membership(room_id, self.member_3) == "join"

    @patch(
        "famedly_control_synapse.client.FamedlyControlClient.get_all_groups_diffs",
        new_callable=AsyncMock,
    )
    def test_sync_add_diff_for_user_already_in_room_via_other_group(
        self, mock_get_diffs
    ) -> None:
        """
        Sync should handle an ADD diff for a user who is already in the room via another
        group without error, leaving the user joined.

        e.g. member_1 is already in the room via group_1. A sync then delivers an ADD
        diff for member_1 from group_2 (which is also assigned to the same room).
        The join should be idempotent.
        """
        room_id = self._create_managed_room_for_sync(
            groups=["test_group_1", "test_group_2"]
        )

        # First sync: member_1 joins via group_1, member_2 joins via group_2
        mock_get_diffs.return_value = ManyGroupsDiffResponse(
            next_sync="1",
            data={
                "test_group_1": [
                    DiffRecord(user_id=self.member_1, action=MembershipAction.ADD),
                ],
                "test_group_2": [
                    DiffRecord(user_id=self.member_2, action=MembershipAction.ADD),
                ],
            },
        )
        self.get_success(self.syncer._process_sync())
        assert self._get_membership(room_id, self.member_1) == "join"
        assert self._get_membership(room_id, self.member_2) == "join"

        # Second sync: member_1 is also added to group_2 (already in room via group_1)
        mock_get_diffs.return_value = ManyGroupsDiffResponse(
            next_sync="2",
            data={
                "test_group_2": [
                    DiffRecord(user_id=self.member_1, action=MembershipAction.ADD),
                ],
            },
        )
        self.get_success(self.syncer._process_sync())

        # member_1 should still be joined
        assert self._get_membership(room_id, self.member_1) == "join"
        # Other members unchanged
        assert self._get_membership(room_id, self.member_2) == "join"
        # Token must have advanced
        assert self.syncer._sync_token == "2"

    @patch(
        "famedly_control_synapse.client.FamedlyControlClient.get_all_groups_diffs",
        new_callable=AsyncMock,
    )
    def test_sync_mixed_add_and_remove(self, mock_get_diffs) -> None:
        """A single diff with both Add and Rem actions should add new users,
        remove specified users, and leave others unchanged."""
        room_id = self._create_managed_room_for_sync(groups=["group_mixed"])

        # Set up initial state: member_1 and member_2 are in the room
        mock_get_diffs.return_value = ManyGroupsDiffResponse(
            next_sync="1",
            data={
                "group_mixed": [
                    DiffRecord(user_id=self.member_1, action=MembershipAction.ADD),
                    DiffRecord(user_id=self.member_2, action=MembershipAction.ADD),
                ],
            },
        )
        self.get_success(self.syncer._process_sync())
        assert self._get_membership(room_id, self.member_1) == "join"
        assert self._get_membership(room_id, self.member_2) == "join"

        # Now add member_3 and remove member_1 in one diff
        mock_get_diffs.return_value = ManyGroupsDiffResponse(
            next_sync="2",
            data={
                "group_mixed": [
                    DiffRecord(user_id=self.member_3, action=MembershipAction.ADD),
                    DiffRecord(user_id=self.member_1, action=MembershipAction.REM),
                ],
            },
        )
        self.get_success(self.syncer._process_sync())

        # Newly added
        assert self._get_membership(room_id, self.member_3) == "join"
        # Removed
        assert self._get_membership(room_id, self.member_1) == "leave"
        # Unchanged
        assert self._get_membership(room_id, self.member_2) == "join"

    @patch(
        "famedly_control_synapse.client.FamedlyControlClient.get_all_groups_diffs",
        new_callable=AsyncMock,
    )
    def test_sync_advances_token_on_success(self, mock_get_diffs) -> None:
        """Sync token should advance after successful processing."""
        self._create_managed_room_for_sync(groups=["group_c"])

        self.syncer._sync_token = None
        mock_get_diffs.return_value = ManyGroupsDiffResponse(
            next_sync="42",
            data={},
        )
        self.get_success(self.syncer._process_sync())

        assert self.syncer._sync_token == "42"
        entry = self.get_success(self.repository.get_sync_token_entry())
        assert entry is not None
        assert entry[1] == "42"

    @patch(
        "famedly_control_synapse.client.FamedlyControlClient.get_all_groups_diffs",
        new_callable=AsyncMock,
    )
    def test_sync_token_persisted_and_restored(self, mock_get_diffs) -> None:
        """Sync token should be persisted to the database and restored on loop start."""
        # Initialize sync token account data by creating a managed room
        self._create_managed_room_for_sync(groups=["group_persist"])
        self.syncer._sync_token_user_id = self.creator
        self.syncer._sync_token = None
        mock_get_diffs.return_value = ManyGroupsDiffResponse(
            next_sync="99",
            data={},
        )
        self.get_success(self.syncer._process_sync())
        assert self.syncer._sync_token == "99"

        # Simulate restart: reset in-memory state and let start_sync_loop restore from DB
        self.syncer._sync_token = None
        self.syncer._sync_token_user_id = None
        self.get_success(self.syncer.start_sync_loop())
        assert self.syncer._sync_token_user_id == self.creator
        assert self.syncer._sync_token == "99"

    @patch(
        "famedly_control_synapse.client.FamedlyControlClient.get_all_groups_diffs",
        new_callable=AsyncMock,
    )
    def test_sync_handles_unknown_user(self, mock_get_diffs) -> None:
        """Sync token can advance if membership updates fail."""
        self._create_managed_room_for_sync(groups=["group_d"])

        self.syncer._sync_token = "5"
        mock_get_diffs.return_value = ManyGroupsDiffResponse(
            next_sync="10",
            data={
                "group_d": [
                    DiffRecord(user_id=self.member_1, action=MembershipAction.ADD),
                ],
            },
        )

        with (
            patch(
                "famedly_control_synapse.room_handler.ManagedRoomHandler.force_join_users_to_room",
                new_callable=AsyncMock,
                return_value={self.member_1: "user not found"},
            ),
        ):
            self.get_success(self.syncer._process_sync())

        assert self.syncer._sync_token == "10"

    @patch(
        "famedly_control_synapse.client.FamedlyControlClient.get_all_groups_diffs",
        new_callable=AsyncMock,
    )
    def test_sync_retries_on_api_error(self, mock_get_diffs) -> None:
        """An API error should be caught by the sync loop without crashing."""
        mock_get_diffs.side_effect = Exception("connection refused")

        self.syncer._sync_token = None

        # _process_sync should propagate the exception (the loop catches it)
        self.get_failure(self.syncer._process_sync(), Exception)

        # Token should remain unchanged
        assert self.syncer._sync_token is None

    @patch(
        "famedly_control_synapse.client.FamedlyControlClient.get_all_groups_diffs",
        new_callable=AsyncMock,
    )
    def test_sync_handles_unknown_group(self, mock_get_diffs) -> None:
        """Diffs for groups not assigned to any managed room should be ignored."""
        self._create_managed_room_for_sync(groups=["group_e"])

        self.syncer._sync_token = None
        mock_get_diffs.return_value = ManyGroupsDiffResponse(
            next_sync="1",
            data={
                "unknown_group": [
                    DiffRecord(user_id=self.member_1, action=MembershipAction.ADD),
                ],
            },
        )
        self.get_success(self.syncer._process_sync())

        # Token should still advance - unknown groups aren't errors
        assert self.syncer._sync_token == "1"

    @patch(
        "famedly_control_synapse.client.FamedlyControlClient.get_all_groups_diffs",
        new_callable=AsyncMock,
    )
    def test_sync_applies_diffs_to_multiple_rooms(self, mock_get_diffs) -> None:
        """If multiple rooms share the same group, diffs should apply to all of them."""
        room_id_1 = self._create_managed_room_for_sync(
            name="Room 1", groups=["shared_group"]
        )
        room_id_2 = self._create_managed_room_for_sync(
            name="Room 2", groups=["shared_group"]
        )

        mock_get_diffs.return_value = ManyGroupsDiffResponse(
            next_sync="1",
            data={
                "shared_group": [
                    DiffRecord(user_id=self.member_1, action=MembershipAction.ADD),
                ],
            },
        )
        self.get_success(self.syncer._process_sync())

        assert self._get_membership(room_id_1, self.member_1) == "join"
        assert self._get_membership(room_id_2, self.member_1) == "join"

    @patch(
        "famedly_control_synapse.client.FamedlyControlClient.get_all_groups_diffs",
        new_callable=AsyncMock,
    )
    def test_sync_empty_data(self, mock_get_diffs) -> None:
        """Empty diff data should be a no-op and advance the token."""
        self.syncer._sync_token = None
        mock_get_diffs.return_value = ManyGroupsDiffResponse(
            next_sync="1",
            data={},
        )
        self.get_success(self.syncer._process_sync())
        assert self.syncer._sync_token == "1"


class TestGroupMembershipSyncLoop(ModuleApiTestCase):
    """
    Test the sync loop for adding and removing members from rooms, long polling
    and gracefully handling errors

    Infrastructure to automatically handle groups in a simulated way
    similar to the external api has been introduced
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
        return conf

    def prepare(self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer):
        super().prepare(reactor, clock, homeserver)
        self.syncer: GroupMembershipSyncer = self.hs.room_control.syncer  # type: ignore[attr-defined]
        self.repository: ManagedRoomRepository = self.hs.room_control.repository  # type: ignore[attr-defined]

        self.member_1 = self.register_user("sync_member_1", "password")
        self.member_2 = self.register_user("sync_member_2", "password")
        self.member_3 = self.register_user("sync_member_3", "password")

        # Let's give ourselves a seed room so the sync loop has the appropriate
        # structures in place
        self.add_or_update_mock_group("seed_group", [self.member_1])

        self.seed_room = self._create_managed_room(groups=["seed_group"])

    def _get_group_members(self, group: str) -> list[str]:
        """Part of the mock infrastructure for the external /get_group_members call"""
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

    def test_loop_sync_token_advances_when_adding_members_to_room(self) -> None:
        """
        Test that the sync token advances when adding members to a room via the sync loop
        """
        # First create a blank room, add the members with the local endpoint afterward
        # Construct a group with a single member
        group_name = "add_member_to_room_test"
        self.add_or_update_mock_group(group_name, [self.member_1])

        room_id = self._create_managed_room()

        # Verify the sync token is where it is expected to be. It should not advance
        # until the loop has been allowed to run
        assert self.syncer._sync_token is not None
        current_sync_token = int(self.syncer._sync_token)

        # Add the group to the room via the local endpoint. No particular reason more
        # than making sure it works as expected
        self.assign_groups_to_room(room_id, [group_name])

        # get the members of the room, should be the room creator and member_1
        self.assert_users_in_room(
            room_id,
            [self.creator, self.member_1],
        )

        # update the group, to reflect that the group has a new member
        self.add_or_update_mock_group(group_name, [self.member_1, self.member_2])

        # Verify the loop has not ran yet
        assert current_sync_token == int(self.syncer._sync_token)

        # This should allow the sync token to advance
        self.wait_for_sync_loop()

        assert current_sync_token < int(self.syncer._sync_token)

        # test for membership changes
        self.assert_users_in_room(
            room_id,
            [self.creator, self.member_1, self.member_2],
        )

    def test_loop_sync_token_advances_when_removing_members_from_room(self) -> None:
        """
        Test that the sync token advances when removing members from room via the sync
        loop
        """
        # Group with two members, add them to the room while creating the room
        group_name = "remove_member_from_room_test"
        self.add_or_update_mock_group(group_name, [self.member_1, self.member_2])

        room_id = self._create_managed_room(groups=[group_name])

        assert self.syncer._sync_token is not None
        current_sync_token = int(self.syncer._sync_token)

        # get the members of the room, should be the room creator, member_1 and member_2
        self.assert_users_in_room(
            room_id,
            [self.creator, self.member_1, self.member_2],
        )

        assert current_sync_token == int(self.syncer._sync_token)

        # update the group, to reflect that the group has removed a new member
        self.add_or_update_mock_group(group_name, [self.member_1])

        # Advance the sync loop until the sync token has changed
        self.wait_for_sync_loop()

        # test for membership changes
        assert current_sync_token < int(self.syncer._sync_token)
        self.assert_users_in_room(
            room_id,
            [self.creator, self.member_1],
        )

    def test_loop_can_long_poll(self) -> None:
        """Test that a sync loop can long poll if no results are returned"""

        # Construct a group with a single member
        group_name = "loop_can_long_poll_test"
        self.add_or_update_mock_group(group_name, [self.member_1])

        # Create a room with that group. That will be the starting point
        room_id = self._create_managed_room(groups=[group_name])
        assert self.syncer._sync_token is not None

        # as we go, verify that the sync token is what we expect it to be. Should only
        # advance after a member is added to a group(in our case, using
        # `add_or_update_mock_group()`)
        current_sync_token = int(self.syncer._sync_token)

        # get the members of the room, should be the room creator and member_1
        self.assert_users_in_room(
            room_id,
            [self.creator, self.member_1],
        )

        # The sync token should not have changed yet
        assert current_sync_token == int(
            self.syncer._sync_token
        ), "sync token changed unexpectedly, check 1"

        # update the group, to reflect that the group has a new member. By itself, this
        # will not update the local sync token. Have to wait for the next iteration of
        # the sync loop
        self.add_or_update_mock_group(group_name, [self.member_1, self.member_2])
        assert current_sync_token == int(
            self.syncer._sync_token
        ), "sync token changed unexpectedly, check 2"

        # I think we loop here, advance and see if the sync loop is picking it up?
        self.wait_for_sync_loop()

        assert current_sync_token < int(
            self.syncer._sync_token
        ), "sync token should have changed, check 3"
        # now reset the sync token assumption, so it can be tested again below
        current_sync_token = int(self.syncer._sync_token)

        # test for membership changes
        self.assert_users_in_room(
            room_id,
            [self.creator, self.member_1, self.member_2],
        )

        # update the group again. This will cause the sync token to advance during the
        # next iteration
        self.add_or_update_mock_group(
            group_name, [self.member_1, self.member_2, self.member_3]
        )
        assert current_sync_token == int(
            self.syncer._sync_token
        ), "sync token changed unexpectedly, check 4"

        self.wait_for_sync_loop()
        assert current_sync_token < int(
            self.syncer._sync_token
        ), "sync token should have changed, check 5"

        # test for membership changes
        self.assert_users_in_room(
            room_id,
            [self.creator, self.member_1, self.member_2, self.member_3],
        )

    def test_loop_can_gracefully_retry_after_error(self) -> None:
        """Test that an error of an expected kind allows the sync loop to try again"""
        # This is largely a copy of test_loop_can_long_poll, with the
        # addition of an Exception we throw into the sync loop at a key spot

        # Construct a group with a single member
        group_name = "loop_can_gracefully_error_test"
        self.add_or_update_mock_group(group_name, [self.member_1])

        # Create a room with that group. That will be the starting point
        room_id = self._create_managed_room(groups=[group_name])
        assert self.syncer._sync_token is not None

        # as we go, verify that the sync token is what we expect it to be. Should only
        # advance after a member is added to a group(in our case, using
        # `add_or_update_mock_group()`) but not until the sync loop has determined so
        current_sync_token = int(self.syncer._sync_token)

        # get the members of the room, should be the room creator and member_1
        self.assert_users_in_room(
            room_id,
            [self.creator, self.member_1],
        )

        # The sync token should not have changed yet
        assert current_sync_token == int(
            self.syncer._sync_token
        ), "sync token changed unexpectedly, check 1"

        # update the group, to reflect that the group has a new member. By itself, this
        # will not update the local sync token. Have to wait for the next iteration of
        # the sync loop
        self.add_or_update_mock_group(group_name, [self.member_1, self.member_2])
        assert current_sync_token == int(
            self.syncer._sync_token
        ), "sync token changed unexpectedly, check 2"

        # Advance the sync loop so it is primed for the next iteration, which will fail
        self.wait_for_sync_loop()
        assert current_sync_token < int(
            self.syncer._sync_token
        ), "sync token changed unexpectedly, check 3"

        # reset the expected token
        current_sync_token = int(self.syncer._sync_token)

        # Throw an Exception into the sync loop. This will hit when a call is made to
        # the external api /get_all_groups_diffs, showcasing a network interruption.
        # During the 'wait_for_sync_loop()' function, it will need to be reset or the
        # error will continue to raise
        self.mock_get_group_diff.side_effect = HttpResponseException(
            404, "Endpoint not found", response=b""
        )

        # advance time to 5 seconds, since that is what the force wait is set to. The
        # sync loop should now be somewhere inside of it's sleep() call
        self.reactor.advance(5.0)

        # Add a new member. This should push the machine forward in preparation of the
        # network call being re-established
        self.add_or_update_mock_group(
            group_name, [self.member_1, self.member_2, self.member_3]
        )
        assert current_sync_token == int(
            self.syncer._sync_token
        ), "sync token changed unexpectedly, check 3"

        # Advance the loop, but remember to clear the exception or the test will not
        # finish. 10 was chosen arbitrarily as a multiple of the config duration
        loop_count = self.wait_for_sync_loop(reset_mock_exception_after=10)

        # 10 iterations was chosen, this should be higher
        assert loop_count > 10
        assert current_sync_token < int(
            self.syncer._sync_token
        ), "sync token should have changed, check 4"

        # test for membership changes
        self.assert_users_in_room(
            room_id,
            [self.creator, self.member_1, self.member_2, self.member_3],
        )
