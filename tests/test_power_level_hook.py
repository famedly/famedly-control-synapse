from http import HTTPStatus

from synapse.api.constants import CREATOR_POWER_LEVEL, EventTypes
from synapse.server import HomeServer
from synapse.util.clock import Clock
from twisted.internet.testing import MemoryReactor

from famedly_control_synapse.types import CreateManagedRoomRequest
from tests.utils.module_api_testcase import ModuleApiTestCase


class TestPowerLevelHook(ModuleApiTestCase):
    CREATE_PATH = "/_famedlyControl/v1/managedRooms/createRoom"

    def prepare(self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer):
        super().prepare(reactor, clock, homeserver)
        self.non_admin = self.register_user("non_admin", "password", admin=False)
        self.non_admin_token = self.login("non_admin", "password")

    def _create_managed_room(self) -> str:
        config = CreateManagedRoomRequest(
            room_alias_name="pl_test_room",
            name="PL Test Room",
            groups=["test_group"],
        )
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

        pl["users"][self.non_admin] = CREATOR_POWER_LEVEL
        self._set_power_levels(room_id, pl, expect_code=HTTPStatus.FORBIDDEN)

    def test_reject_other_user_higher_than_admin(self) -> None:
        """Setting another user's PL higher than the admin's should be rejected."""
        room_id = self._create_managed_room()
        pl = self._get_power_levels(room_id)

        pl["users"][self.non_admin] = CREATOR_POWER_LEVEL + 1
        self._set_power_levels(room_id, pl, expect_code=HTTPStatus.FORBIDDEN)

    def test_reject_users_default_equal_to_admin(self) -> None:
        """Setting users_default equal to admin PL should be rejected."""
        room_id = self._create_managed_room()
        pl = self._get_power_levels(room_id)

        pl["users_default"] = CREATOR_POWER_LEVEL
        self._set_power_levels(room_id, pl, expect_code=HTTPStatus.FORBIDDEN)

    def test_reject_ban_level_below_non_admin(self) -> None:
        """Setting ban PL at or below a non-admin user's PL should be rejected."""
        room_id = self._create_managed_room()
        pl = self._get_power_levels(room_id)

        pl["users"][self.non_admin] = 50
        pl["ban"] = 50
        self._set_power_levels(room_id, pl, expect_code=HTTPStatus.FORBIDDEN)

    def test_reject_kick_level_below_non_admin(self) -> None:
        """Setting kick PL at or below a non-admin user's PL should be rejected."""
        room_id = self._create_managed_room()
        pl = self._get_power_levels(room_id)

        pl["users"][self.non_admin] = 50
        pl["kick"] = 50
        self._set_power_levels(room_id, pl, expect_code=HTTPStatus.FORBIDDEN)

    def test_reject_invite_level_below_non_admin(self) -> None:
        """Setting invite PL at or below a non-admin user's PL should be rejected."""
        room_id = self._create_managed_room()
        pl = self._get_power_levels(room_id)

        pl["users"][self.non_admin] = 50
        pl["invite"] = 50
        self._set_power_levels(room_id, pl, expect_code=HTTPStatus.FORBIDDEN)

    def test_reject_membership_level_below_users_default(self) -> None:
        """Membership action PLs must be above users_default too."""
        room_id = self._create_managed_room()
        pl = self._get_power_levels(room_id)

        pl["users_default"] = 30
        pl["ban"] = 30
        self._set_power_levels(room_id, pl, expect_code=HTTPStatus.FORBIDDEN)

    def test_allow_valid_power_level_change(self) -> None:
        """A valid PL change that maintains all constraints should be allowed."""
        room_id = self._create_managed_room()
        pl = self._get_power_levels(room_id)

        pl["users"][self.non_admin] = 10
        pl["ban"] = 100
        pl["kick"] = 100
        pl["invite"] = 100
        self._set_power_levels(room_id, pl, expect_code=HTTPStatus.OK)

    def test_allow_membership_levels_above_non_admin(self) -> None:
        """Membership action PLs strictly above all non-admin users should be allowed."""
        room_id = self._create_managed_room()
        pl = self._get_power_levels(room_id)

        pl["users"][self.non_admin] = 50
        pl["ban"] = 51
        pl["kick"] = 51
        pl["invite"] = 51
        self._set_power_levels(room_id, pl, expect_code=HTTPStatus.OK)

    def test_reject_admin_lower_than_other_user(self) -> None:
        """Admin lowering their PL below another user should be rejected."""
        room_id = self._create_managed_room()
        pl = self._get_power_levels(room_id)

        # First give non_admin a PL, then try to lower admin below it
        pl["users"][self.non_admin] = 50
        pl["ban"] = 51
        pl["kick"] = 51
        pl["invite"] = 51
        self._set_power_levels(room_id, pl, expect_code=HTTPStatus.OK)

        pl = self._get_power_levels(room_id)
        pl["users"][self.creator] = 49
        self._set_power_levels(room_id, pl, expect_code=HTTPStatus.FORBIDDEN)

    def test_reject_non_admin_at_sensitive_event_threshold(self) -> None:
        """Non-admin user with PL >= sensitive event threshold should be rejected,
        even if ban/kick/invite thresholds are set higher."""
        room_id = self._create_managed_room()
        pl = self._get_power_levels(room_id)

        # Set high ban/kick/invite thresholds to attempt bypass
        pl["ban"] = 1000
        pl["kick"] = 1000
        pl["invite"] = 1000
        # Give non-admin PL that is below membership thresholds but at state_default
        pl["users"][self.non_admin] = pl.get("state_default", 100)
        self._set_power_levels(room_id, pl, expect_code=HTTPStatus.FORBIDDEN)

    def test_admin_can_update_power_levels(self) -> None:
        """The room admin should be able to update power levels."""
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
