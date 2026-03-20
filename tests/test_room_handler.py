from http import HTTPStatus
from unittest.mock import AsyncMock, patch

import parameterized
from synapse.api.errors import Codes, SynapseError
from synapse.server import HomeServer
from synapse.util.clock import Clock
from twisted.internet.testing import MemoryReactor

from famedly_control_synapse.rest.types import CreateManagedRoomRequest
from famedly_control_synapse.room_handler import famedly_control_user_sync_error
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
                requester=self.requester,
            )
        )
        assert not error, f"Expected no errors, but got: {error}"
        self._check_users_joined_to_room(room_id, [user_a, user_b, user_c])

    def test_force_join_users_already_in_room(self) -> None:
        """Test that force joining users who are already in the room is handled correctly."""
        user_a = self.register_user("test_user_a", "password")
        user_b = self.register_user("test_user_b", "password")
        room_id = self._create_managed_room_without_members()

        # Force join the same user twice
        error = self.get_success(
            self.room_handler.force_join_users_to_room(
                room_id=room_id, user_mxids=[user_a, user_b], requester=self.requester
            )
        )
        assert not error, f"Expected no errors, but got: {error}"
        self._check_users_joined_to_room(room_id, [user_a, user_b])

        error = self.get_success(
            self.room_handler.force_join_users_to_room(
                room_id=room_id, user_mxids=[user_a, user_b], requester=self.requester
            )
        )
        assert not error, f"Expected no errors, but got: {error}"
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
                room_id=room_id, user_mxids=[user_a, user_b], requester=self.requester
            )
        )
        # Should return an error dict with user_b (the banned user)
        assert error, "Expected errors, but got none"
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
                requester=self.requester,
            )
        )
        # Should return an error dict with the non-existent user
        assert error, "Expected errors, but got none"
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
                room_id=room_id, user_mxids=[user_a, user_b], requester=self.requester
            )
        )
        assert not error, f"Expected no errors, but got: {error}"
        self._check_users_joined_to_room(room_id, [user_a, user_b])

        # Now remove the users from the room
        error = self.get_success(
            self.room_handler.remove_users_from_room(
                creator_id=self.creator, user_mxids=[user_a, user_b], room_id=room_id
            )
        )
        assert not error, f"Expected no errors, but got: {error}"
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
                room_id=room_id, user_mxids=[user_a], requester=self.requester
            )
        )
        assert not error, f"Expected no errors, but got: {error}"
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
        assert error, "Expected errors, but got none"
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
                room_id=room_id, user_mxids=[user_a], requester=self.requester
            )
        )
        assert not error, f"Expected no errors, but got: {error}"
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
        assert not error
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
                room_id=room_id, user_mxids=[user_a], requester=self.requester
            )
        )
        assert not error, f"Expected no errors, but got: {error}"
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
            assert error, "Expected errors, but got None"
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
        result, not_found = self.get_success(
            self.room_handler.batch_convert_external_user_ids_to_matrix_user_ids(
                external_user_ids
            )
        )
        assert result == matrix_user_ids
        assert not_found == []
        assert len(result) == 105

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

        result, not_founds = self.get_success(
            self.room_handler.batch_convert_external_user_ids_to_matrix_user_ids(
                external_user_ids
            )
        )

        assert len(result) == 2
        assert len(not_founds) == 2
        assert f"@existing_1:{self.server_name_for_this_server}" in result
        assert f"@existing_2:{self.server_name_for_this_server}" in result
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
