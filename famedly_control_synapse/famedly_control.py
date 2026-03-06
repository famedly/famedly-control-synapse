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

from synapse.api.constants import EventTypes, Membership
from synapse.api.errors import Codes, SynapseError
from synapse.event_auth import get_user_power_level
from synapse.events import EventBase
from synapse.http.server import JsonResource
from synapse.module_api import ModuleApi
from synapse.types import StateMap

from famedly_control_synapse.client import FamedlyControlClient
from famedly_control_synapse.config import FamedlyControlConfig
from famedly_control_synapse.repository import ManagedRoomRepository
from famedly_control_synapse.rest.room import (
    MANAGED_ROOM_API_PREFIX,
    AssignGroupsToManagedRoomResource,
    CreateManagedRoomResource,
    ListManagedRoomsResource,
)
from famedly_control_synapse.room_handler import ManagedRoomHandler

logger = logging.getLogger(__name__)


class FamedlyControl:
    __version__ = "0.0.1"

    def __init__(self, config: FamedlyControlConfig, api: ModuleApi) -> None:
        self.api = api
        self.server_name = api.server_name
        self.clock = api._hs.get_clock()
        self.config = config
        self.client = FamedlyControlClient(self.api, config)
        self.room_handler = ManagedRoomHandler(self.api, self.config)
        self.repository = ManagedRoomRepository(api)

        # Register servlets
        self.resource = JsonResource(self.api._hs)
        CreateManagedRoomResource(self.api, self.client, self.room_handler).register(
            self.resource
        )
        ListManagedRoomsResource(self.api, self.repository).register(self.resource)
        AssignGroupsToManagedRoomResource(
            self.api, self.client, self.room_handler, self.repository
        ).register(self.resource)
        self.api.register_web_resource(MANAGED_ROOM_API_PREFIX, self.resource)

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
        """Third-party rules callback that enforces membership and power level
        restrictions for managed rooms.
        """
        if event.type not in (EventTypes.Member, EventTypes.PowerLevels):
            return True, None

        create_event = state_events.get((EventTypes.Create, ""))
        if create_event is None:
            raise SynapseError(
                500,
                "Managed room is missing m.room.create state",
                Codes.UNKNOWN,
            )

        if not await self.repository.is_managed_room(
            event.room_id, create_event.sender
        ):
            return True, None

        if event.type == EventTypes.Member:
            return await self._check_membership_allowed(event, create_event.sender)
        elif event.type == EventTypes.PowerLevels:
            return await self._check_power_levels_allowed(
                event, state_events, create_event.sender
            )
        else:
            # cannot happen but makes the linter happy
            return True, None

    async def _check_membership_allowed(
        self, event: EventBase, admin_user: str
    ) -> tuple[bool, dict | None]:
        """Block membership changes in managed rooms unless sent by the admin.

        Joins are allowed because managed rooms are invite-only and only
        the admin can send invites, so a join is implicitly admin-authorised.
        """
        if event.sender == admin_user:
            return True, None

        membership = event.content.get("membership")
        if membership == Membership.JOIN:
            return True, None

        raise SynapseError(
            403,
            "Only the admin can edit membership in a managed room",
            Codes.FORBIDDEN,
        )

    async def _check_power_levels_allowed(
        self, event: EventBase, state_events: StateMap[EventBase], admin_user: str
    ) -> tuple[bool, dict | None]:
        """Block invalid power level changes in managed rooms.

        Enforced constraints:
        * The admin must remain the highest power level in the room.
        * No non-admin user may have a PL >= the admin's PL.
        * ``users_default`` must be strictly below the admin's PL.
        * Non-admin users must stay below sensitive event thresholds
          (``state_default``, ``m.room.power_levels``).
        * Membership actions (ban, kick, invite) must not exceed the
          admin's PL and must be strictly above all non-admin users.
        """
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
