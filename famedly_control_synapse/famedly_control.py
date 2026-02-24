# Copyright (C) 2026 Famedly
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
import logging
from typing import Any

from synapse.api.constants import EventTypes
from synapse.api.errors import Codes, SynapseError
from synapse.event_auth import get_user_power_level
from synapse.events import EventBase
from synapse.module_api import ModuleApi
from synapse.types import StateMap

from famedly_control_synapse.config import FamedlyControlConfig
from famedly_control_synapse.repository import ManagedRoomRepository
from famedly_control_synapse.rest.room import (
    MANAGED_ROOM_API_PREFIX,
    CreateManagedRoomResource,
    ListManagedRoomsResource,
    ManagedRoomResource,
)

logger = logging.getLogger(__name__)


class FamedlyControl:
    __version__ = "0.0.1"

    def __init__(self, config: FamedlyControlConfig, api: ModuleApi) -> None:
        self.api = api
        self.server_name = api.server_name
        self.clock = api._hs.get_clock()
        self.config = config
        self.repository = ManagedRoomRepository(api)

        root_resource = ManagedRoomResource(self.api, self.config)
        root_resource.putChild(
            b"createRoom", CreateManagedRoomResource(self.api, self.config)
        )
        root_resource.putChild(
            b"rooms", ListManagedRoomsResource(self.api, self.config)
        )
        self.api.register_web_resource(MANAGED_ROOM_API_PREFIX, root_resource)

        self.api.register_third_party_rules_callbacks(
            check_event_allowed=self.check_event_allowed,
        )

        logger.info("Module initialized")

    @staticmethod
    def parse_config(config: dict[str, Any]) -> FamedlyControlConfig:
        return FamedlyControlConfig.model_validate(config)

    async def check_event_allowed(
        self, event: EventBase, state_events: StateMap[EventBase]
    ) -> tuple[bool, dict | None]:
        """Enforce power level restrictions for managed rooms.

        * The admin must remain the highest power level in the room.
        * No non-admin user may have a PL >= the admin's new PL.
        * ``users_default`` must be strictly below the admin's new PL.
        * Non-admin users must stay below sensitive event thresholds
          (``state_default``, ``m.room.power_levels``).
        * Membership actions (ban, kick, invite) must not exceed the
          admin's PL and must be strictly above all non-admin users.
        """
        if event.type != EventTypes.PowerLevels:
            return True, None

        if not await self.repository.is_managed_room(event.room_id):
            return True, None

        create_event = state_events.get((EventTypes.Create, ""))
        if create_event is None:
            return True, None

        admin_user = create_event.sender

        new_content = event.content
        users = new_content.get("users", {})
        users_default = new_content.get("users_default", 0)
        # the admin isn't necessarily present in the users list for power level updates
        # this is the case for rooms >v12
        current_admin_pl = get_user_power_level(admin_user, state_events)
        admin_pl = users.get(admin_user, current_admin_pl)

        for user_id, pl in users.items():
            if user_id != admin_user and pl >= admin_pl:
                raise SynapseError(
                    403,
                    "No user can have a power level equal to or higher than the admin in a managed room",
                    Codes.FORBIDDEN,
                )

        if users_default >= admin_pl:
            raise SynapseError(
                403,
                "users_default cannot be equal to or higher than the admin power level in a managed room",
                Codes.FORBIDDEN,
            )

        # Prevent non-admin users from reaching sensitive event thresholds.
        state_default = new_content.get("state_default", 50)
        events = new_content.get("events", {})
        power_levels_pl = events.get(EventTypes.PowerLevels, state_default)
        sensitive_threshold = min(state_default, power_levels_pl)

        max_non_admin_pl = users_default
        for uid, pl in users.items():
            if uid != admin_user and pl > max_non_admin_pl:
                max_non_admin_pl = pl

        if users_default >= sensitive_threshold:
            raise SynapseError(
                403,
                "users_default cannot be equal to or above the threshold for sensitive state events in a managed room",
                Codes.FORBIDDEN,
            )

        for user_id, pl in users.items():
            if user_id != admin_user and pl >= sensitive_threshold:
                raise SynapseError(
                    403,
                    "non-admin user power level cannot be equal to or above the threshold for sensitive state events in a managed room",
                    Codes.FORBIDDEN,
                )

        # membership actions default to Matrix-spec values when not explicitly set.
        membership_action_defaults = {
            "ban": 50,
            "kick": 50,
            "invite": 50,
        }
        for action in ("ban", "kick", "invite"):
            action_pl = new_content.get(action)
            if action_pl is None:
                action_pl = membership_action_defaults[action]

            if action_pl > admin_pl:
                raise SynapseError(
                    403,
                    f"{action} power level cannot exceed admin power level in a managed room",
                    Codes.FORBIDDEN,
                )

            if action_pl <= max_non_admin_pl:
                raise SynapseError(
                    403,
                    f"{action} power level must be higher than all non-admin users in a managed room",
                    Codes.FORBIDDEN,
                )

        return True, None
