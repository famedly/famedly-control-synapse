from http import HTTPStatus
from unittest.mock import AsyncMock, patch

from synapse.server import HomeServer
from synapse.util.clock import Clock
from twisted.internet.testing import MemoryReactor

from famedly_control_synapse.client import (
    DiffRecord,
    ManyGroupsDiffResponse,
    Membership,
)
from famedly_control_synapse.repository import ManagedRoomRepository
from famedly_control_synapse.rest.types import CreateManagedRoomRequest
from famedly_control_synapse.sync import GroupMembershipSyncer
from tests.utils.module_api_testcase import ModuleApiTestCase


class TestGroupMembershipSync(ModuleApiTestCase):
    def default_config(self):
        conf = super().default_config()
        conf["modules"][0]["config"]["sync_enabled"] = True
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
            patch(
                "famedly_control_synapse.room_handler.ManagedRoomHandler.batch_convert_external_user_ids_to_matrix_user_ids",
                new_callable=AsyncMock,
                side_effect=lambda x: (x, []),
            ),
        ):
            config = CreateManagedRoomRequest(
                room_alias_name=f"sync_room_{self._room_counter + 1}",
                name=name,
                groups=groups,
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
        "famedly_control_synapse.room_handler.ManagedRoomHandler.batch_convert_external_user_ids_to_matrix_user_ids",
        new_callable=AsyncMock,
    )
    @patch(
        "famedly_control_synapse.client.FamedlyControlClient.get_all_groups_diffs",
        new_callable=AsyncMock,
    )
    def test_sync_adds_users_to_room(self, mock_get_diffs, mock_batch_convert) -> None:
        """Sync should add new users while leaving existing members unchanged."""
        room_id = self._create_managed_room_for_sync(groups=["group_a"])
        mock_batch_convert.side_effect = lambda x: (x, [])

        # First sync: add member_1 as an existing member
        mock_get_diffs.return_value = ManyGroupsDiffResponse(
            next_sync="1",
            data={
                "group_a": [
                    DiffRecord(user_id=self.member_1, action=Membership.ADD),
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
                    DiffRecord(user_id=self.member_2, action=Membership.ADD),
                    DiffRecord(user_id=self.member_3, action=Membership.ADD),
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
        "famedly_control_synapse.room_handler.ManagedRoomHandler.batch_convert_external_user_ids_to_matrix_user_ids",
        new_callable=AsyncMock,
    )
    @patch(
        "famedly_control_synapse.client.FamedlyControlClient.get_all_groups_diffs",
        new_callable=AsyncMock,
    )
    def test_sync_removes_users_from_room(
        self, mock_get_diffs, mock_batch_convert
    ) -> None:
        """Sync should remove specified users while leaving others unchanged."""
        room_id = self._create_managed_room_for_sync(groups=["group_b"])
        mock_batch_convert.side_effect = lambda x: (x, [])

        # First add all three members
        mock_get_diffs.return_value = ManyGroupsDiffResponse(
            next_sync="1",
            data={
                "group_b": [
                    DiffRecord(user_id=self.member_1, action=Membership.ADD),
                    DiffRecord(user_id=self.member_2, action=Membership.ADD),
                    DiffRecord(user_id=self.member_3, action=Membership.ADD),
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
                    DiffRecord(user_id=self.member_1, action=Membership.REM),
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
        "famedly_control_synapse.room_handler.ManagedRoomHandler.batch_convert_external_user_ids_to_matrix_user_ids",
        new_callable=AsyncMock,
    )
    @patch(
        "famedly_control_synapse.client.FamedlyControlClient.get_all_groups_diffs",
        new_callable=AsyncMock,
    )
    def test_sync_mixed_add_and_remove(
        self, mock_get_diffs, mock_batch_convert
    ) -> None:
        """A single diff with both Add and Rem actions should add new users,
        remove specified users, and leave others unchanged."""
        room_id = self._create_managed_room_for_sync(groups=["group_mixed"])
        mock_batch_convert.side_effect = lambda x: (x, [])

        # Set up initial state: member_1 and member_2 are in the room
        mock_get_diffs.return_value = ManyGroupsDiffResponse(
            next_sync="1",
            data={
                "group_mixed": [
                    DiffRecord(user_id=self.member_1, action=Membership.ADD),
                    DiffRecord(user_id=self.member_2, action=Membership.ADD),
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
                    DiffRecord(user_id=self.member_3, action=Membership.ADD),
                    DiffRecord(user_id=self.member_1, action=Membership.REM),
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
        "famedly_control_synapse.room_handler.ManagedRoomHandler.batch_convert_external_user_ids_to_matrix_user_ids",
        new_callable=AsyncMock,
    )
    @patch(
        "famedly_control_synapse.client.FamedlyControlClient.get_all_groups_diffs",
        new_callable=AsyncMock,
    )
    def test_sync_advances_token_on_success(
        self, mock_get_diffs, mock_batch_convert
    ) -> None:
        """Sync token should advance after successful processing."""
        self._create_managed_room_for_sync(groups=["group_c"])
        mock_batch_convert.side_effect = lambda x: (x, [])

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
        with patch.object(self.syncer.api, "looping_background_call"):
            self.get_success(self.syncer.start_sync_loop())
        assert self.syncer._sync_token_user_id == self.creator
        assert self.syncer._sync_token == "99"

    @patch(
        "famedly_control_synapse.client.FamedlyControlClient.get_all_groups_diffs",
        new_callable=AsyncMock,
    )
    def test_sync_does_not_advance_token_on_failure(self, mock_get_diffs) -> None:
        """Sync token should NOT advance if membership updates fail."""
        self._create_managed_room_for_sync(groups=["group_d"])

        self.syncer._sync_token = "5"
        mock_get_diffs.return_value = ManyGroupsDiffResponse(
            next_sync="10",
            data={
                "group_d": [
                    DiffRecord(user_id=self.member_1, action=Membership.ADD),
                ],
            },
        )

        with (
            patch(
                "famedly_control_synapse.room_handler.ManagedRoomHandler.batch_convert_external_user_ids_to_matrix_user_ids",
                new_callable=AsyncMock,
                return_value=([self.member_1], []),
            ),
            patch(
                "famedly_control_synapse.room_handler.ManagedRoomHandler.force_join_users_to_room",
                new_callable=AsyncMock,
                return_value={self.member_1: "user not found"},
            ),
        ):
            self.get_success(self.syncer._process_sync())

        assert self.syncer._sync_token == "5"

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
        "famedly_control_synapse.room_handler.ManagedRoomHandler.batch_convert_external_user_ids_to_matrix_user_ids",
        new_callable=AsyncMock,
    )
    @patch(
        "famedly_control_synapse.client.FamedlyControlClient.get_all_groups_diffs",
        new_callable=AsyncMock,
    )
    def test_sync_handles_unknown_group(
        self, mock_get_diffs, mock_batch_convert
    ) -> None:
        """Diffs for groups not assigned to any managed room should be ignored."""
        self._create_managed_room_for_sync(groups=["group_e"])
        mock_batch_convert.side_effect = lambda x: (x, [])

        self.syncer._sync_token = None
        mock_get_diffs.return_value = ManyGroupsDiffResponse(
            next_sync="1",
            data={
                "unknown_group": [
                    DiffRecord(user_id=self.member_1, action=Membership.ADD),
                ],
            },
        )
        self.get_success(self.syncer._process_sync())

        # Token should still advance - unknown groups aren't errors
        assert self.syncer._sync_token == "1"

    @patch(
        "famedly_control_synapse.room_handler.ManagedRoomHandler.batch_convert_external_user_ids_to_matrix_user_ids",
        new_callable=AsyncMock,
    )
    @patch(
        "famedly_control_synapse.client.FamedlyControlClient.get_all_groups_diffs",
        new_callable=AsyncMock,
    )
    def test_sync_applies_diffs_to_multiple_rooms(
        self, mock_get_diffs, mock_batch_convert
    ) -> None:
        """If multiple rooms share the same group, diffs should apply to all of them."""
        room_id_1 = self._create_managed_room_for_sync(
            name="Room 1", groups=["shared_group"]
        )
        room_id_2 = self._create_managed_room_for_sync(
            name="Room 2", groups=["shared_group"]
        )
        mock_batch_convert.side_effect = lambda x: (x, [])

        mock_get_diffs.return_value = ManyGroupsDiffResponse(
            next_sync="1",
            data={
                "shared_group": [
                    DiffRecord(user_id=self.member_1, action=Membership.ADD),
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
