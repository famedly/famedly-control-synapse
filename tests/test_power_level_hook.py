from http import HTTPStatus
from unittest.mock import AsyncMock, patch

import parameterized
from synapse.api.constants import CREATOR_POWER_LEVEL, EventTypes
from synapse.server import HomeServer
from synapse.util.clock import Clock
from twisted.internet.testing import MemoryReactor

from famedly_control_synapse.rest.types import CreateManagedRoomRequest
from tests.utils.module_api_testcase import ModuleApiTestCase


@parameterized.parameterized_class(("room_version",), [("10",), ("12",)])
class TestPowerLevelHook(ModuleApiTestCase):
    """
    Test that trying to change the invariant power levels of an existing room is:
    1. Prohibited by a non-room creator admin
    2. Isn't allowed if any other user is given the same power level of the room creator
    3. Isn't allowed if any sensitive event types are changed
    4. Isn't allowed if any membership actions are changed
    5. Is allowed for normal room administrator behavior(nothing unexpected broke)
    """

    room_version: str
    CREATE_PATH = "/_famedlyControl/v1/managedRooms/createRoom"

    def prepare(self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer):
        super().prepare(reactor, clock, homeserver)
        self.non_admin = self.register_user("non_admin", "password", admin=False)
        self.non_admin_token = self.login("non_admin", "password")

    def _create_managed_room(
        self, name: str = "PL Test Room", groups: list[str] | None = None
    ) -> str:
        config = CreateManagedRoomRequest(
            room_alias_name="pl_test_room",
            name=name,
            room_version=self.room_version,
            groups=groups or ["test_group"],
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

    def _get_power_levels(self, room_id: str) -> dict:
        return self.helper.get_state(
            room_id, EventTypes.PowerLevels, self.creator_access_token
        )

    def _set_power_levels(
        self, room_id: str, content: dict, expect_code: int = HTTPStatus.OK
    ) -> dict:
        return self.helper.send_state(
            room_id,
            EventTypes.PowerLevels,
            content,
            self.creator_access_token,
            expect_code=expect_code,
        )

    def test_reject_other_user_equal_to_admin(self) -> None:
        """Setting another user's PL equal to the admin's should be rejected."""
        room_id = self._create_managed_room()
        pl = self._get_power_levels(room_id)

        pl["users"][self.non_admin] = CREATOR_POWER_LEVEL - 1
        self._set_power_levels(room_id, pl, expect_code=HTTPStatus.FORBIDDEN)

    def test_reject_other_user_higher_than_admin(self) -> None:
        """Setting another user's PL higher than the admin's should be rejected."""
        room_id = self._create_managed_room()
        pl = self._get_power_levels(room_id)

        pl["users"][self.non_admin] = CREATOR_POWER_LEVEL
        # CREATOR_POWER_LEVEL is non-canonicaljson, so is rejected
        self._set_power_levels(room_id, pl, expect_code=HTTPStatus.BAD_REQUEST)

    def test_reject_room_creator_cannot_lower_own_power_level(self) -> None:
        """Setting a room creator's power level too low should be rejected"""
        room_id = self._create_managed_room()
        pl = self._get_power_levels(room_id)

        pl["users"][self.creator] = CREATOR_POWER_LEVEL - 2

        # Room versions above "11" do not allow for the room creator to exist in the
        # power level event's 'users' object, as they will always have an 'infinite'
        # level. The response there is different for rooms "11" and below. However, the
        # validation done on the power level event takes place before that check, so
        # instead of raising a 400 it will be the 403
        self._set_power_levels(room_id, pl, expect_code=HTTPStatus.FORBIDDEN)

    @parameterized.parameterized.expand([("ban",), ("kick",), ("invite",)])
    def test_membership_action_power_level_equal_to_admin_no_op(
        self, action_level: str
    ) -> None:
        """Setting specific action PL at admin user's PL should be allowed."""
        room_id = self._create_managed_room()
        pl = self._get_power_levels(room_id)

        pl[action_level] = CREATOR_POWER_LEVEL - 1
        self._set_power_levels(room_id, pl, expect_code=HTTPStatus.OK)

    @parameterized.parameterized.expand([("ban",), ("kick",), ("invite",)])
    def test_membership_action_power_level_below_admin_rejected(
        self, action_level: str
    ) -> None:
        """Setting specific action PL below admin user's PL should be rejected."""
        room_id = self._create_managed_room()
        pl = self._get_power_levels(room_id)

        pl[action_level] = CREATOR_POWER_LEVEL - 2
        self._set_power_levels(room_id, pl, expect_code=HTTPStatus.FORBIDDEN)

    def test_admin_can_update_users_power_levels(self) -> None:
        """The room admin should be able to update power levels."""
        # Or, make sure we did not break setting user's power levels in a normal flow
        room_id = self._create_managed_room()
        pl = self._get_power_levels(room_id)

        pl["users"][self.non_admin] = 10
        self._set_power_levels(room_id, pl, expect_code=HTTPStatus.OK)

    def test_non_admin_cannot_update_power_levels(self) -> None:
        """A non-admin user in the room should not be able to update power levels."""
        room_id = self._create_managed_room()

        # Invite and join the non-admin to the room
        self.helper.invite(
            room_id,
            src=self.creator,
            targ=self.non_admin,
            tok=self.creator_access_token,
        )
        self.helper.join(room_id, user=self.non_admin, tok=self.non_admin_token)

        pl = self._get_power_levels(room_id)
        pl["users"][self.non_admin] = 10
        self.helper.send_state(
            room_id,
            EventTypes.PowerLevels,
            pl,
            self.non_admin_token,
            expect_code=HTTPStatus.FORBIDDEN,
        )

    def test_non_managed_room_not_affected(self) -> None:
        """Power level changes in non-managed rooms should not be affected by the hook."""
        room_id = self.helper.create_room_as(
            self.creator, tok=self.creator_access_token, is_public=False
        )
        pl = self.helper.get_state(
            room_id, EventTypes.PowerLevels, self.creator_access_token
        )

        pl["users"][self.non_admin] = pl["users"].get(self.creator, 100)
        self.helper.send_state(
            room_id,
            EventTypes.PowerLevels,
            pl,
            self.creator_access_token,
            expect_code=HTTPStatus.OK,
        )
