from http import HTTPStatus
from unittest.mock import AsyncMock, patch

import parameterized
from synapse.server import HomeServer
from synapse.util.clock import Clock
from twisted.internet.testing import MemoryReactor

from famedly_control_synapse.rest.types import CreateManagedRoomRequest
from tests.utils.module_api_testcase import ModuleApiTestCase


@parameterized.parameterized_class(("room_version",), [("10",), ("12",)])
class TestMembershipHook(ModuleApiTestCase):
    room_version: str

    def prepare(self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer):
        super().prepare(reactor, clock, homeserver)
        self.non_admin = self.register_user("non_admin", "password", admin=False)
        self.non_admin_token = self.login("non_admin", "password")

    def _create_managed_room(
        self, name: str = "Membership Test Room", groups: list[str] | None = None
    ) -> str:
        config = CreateManagedRoomRequest(
            room_alias_name="membership_test_room",
            name=name,
            groups=groups or ["test_group"],
            room_version=self.room_version,
        )
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
            channel = self.make_request(
                method="POST",
                path=self.CREATE_PATH,
                content=config.model_dump(),
                access_token=self.creator_access_token,
                shorthand=False,
            )
        assert channel.code == HTTPStatus.OK, channel.result
        return channel.json_body["room_id"]

    def _create_managed_room_with_non_admin(self) -> str:
        """Create a managed room and add the non-admin user to it."""
        room_id = self._create_managed_room()
        self.helper.invite(
            room_id,
            src=self.creator,
            targ=self.non_admin,
            tok=self.creator_access_token,
        )
        self.helper.join(room_id, user=self.non_admin, tok=self.non_admin_token)
        return room_id

    def test_admin_can_invite(self) -> None:
        """The admin should be able to invite users to a managed room."""
        room_id = self._create_managed_room()
        self.helper.invite(
            room_id,
            src=self.creator,
            targ=self.non_admin,
            tok=self.creator_access_token,
            expect_code=HTTPStatus.OK,
        )

    def test_non_admin_cannot_invite(self) -> None:
        """A non-admin user should not be able to invite others to a managed room."""
        room_id = self._create_managed_room_with_non_admin()
        other_user = self.register_user("other_user", "password", admin=False)
        self.helper.invite(
            room_id,
            src=self.non_admin,
            targ=other_user,
            tok=self.non_admin_token,
            expect_code=HTTPStatus.FORBIDDEN,
        )

    def test_non_admin_cannot_leave(self) -> None:
        """A non-admin user should not be able to leave a managed room."""
        room_id = self._create_managed_room_with_non_admin()
        self.helper.leave(
            room_id,
            user=self.non_admin,
            tok=self.non_admin_token,
            expect_code=HTTPStatus.FORBIDDEN,
        )

    def test_admin_can_ban(self) -> None:
        """The admin should be able to ban users from a managed room."""
        room_id = self._create_managed_room_with_non_admin()
        self.helper.ban(
            room_id,
            src=self.creator,
            targ=self.non_admin,
            tok=self.creator_access_token,
            expect_code=HTTPStatus.OK,
        )

    def test_non_admin_cannot_ban(self) -> None:
        """A non-admin user should not be able to ban others in a managed room."""
        room_id = self._create_managed_room_with_non_admin()
        other_user = self.register_user("ban_target", "password", admin=False)
        # Admin adds the other user first
        self.helper.invite(
            room_id,
            src=self.creator,
            targ=other_user,
            tok=self.creator_access_token,
        )
        # Non-admin tries to ban
        self.helper.ban(
            room_id,
            src=self.non_admin,
            targ=other_user,
            tok=self.non_admin_token,
            expect_code=HTTPStatus.FORBIDDEN,
        )

    def test_admin_can_kick(self) -> None:
        """The admin should be able to kick users from a managed room."""
        room_id = self._create_managed_room_with_non_admin()
        self.helper.change_membership(
            room_id,
            src=self.creator,
            targ=self.non_admin,
            tok=self.creator_access_token,
            membership="leave",
            expect_code=HTTPStatus.OK,
        )

    def test_non_invited_user_cannot_join(self) -> None:
        """A user who has not been invited should not be able to join a managed room."""
        room_id = self._create_managed_room()
        self.helper.join(
            room_id,
            user=self.non_admin,
            tok=self.non_admin_token,
            expect_code=HTTPStatus.FORBIDDEN,
        )

    def test_non_admin_cannot_kick(self) -> None:
        """A non-admin user should not be able to kick others in a managed room."""
        room_id = self._create_managed_room_with_non_admin()
        other_user = self.register_user("kick_target", "password", admin=False)
        # Admin adds the other user first
        self.helper.invite(
            room_id,
            src=self.creator,
            targ=other_user,
            tok=self.creator_access_token,
        )
        # Non-admin tries to kick
        self.helper.change_membership(
            room_id,
            src=self.non_admin,
            targ=other_user,
            tok=self.non_admin_token,
            membership="leave",
            expect_code=HTTPStatus.FORBIDDEN,
        )

    def test_non_managed_room_not_affected(self) -> None:
        """Membership changes in non-managed rooms should not be restricted."""
        room_id = self.helper.create_room_as(
            self.creator, tok=self.creator_access_token, is_public=False
        )
        self.helper.invite(
            room_id,
            src=self.creator,
            targ=self.non_admin,
            tok=self.creator_access_token,
        )
        self.helper.join(
            room_id,
            user=self.non_admin,
            tok=self.non_admin_token,
            expect_code=HTTPStatus.OK,
        )
        self.helper.leave(
            room_id,
            user=self.non_admin,
            tok=self.non_admin_token,
            expect_code=HTTPStatus.OK,
        )
