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

from pydantic import ValidationError
from synapse.api.constants import EventTypes, Membership
from synapse.events import EventBase
from synapse.http.server import JsonResource
from synapse.module_api import ModuleApi
from synapse.module_api.errors import Codes, SynapseError
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
from famedly_control_synapse.rest.types import PowerLevelEventContent
from famedly_control_synapse.room_handler import ManagedRoomHandler
from famedly_control_synapse.sync import GroupMembershipSyncer

logger = logging.getLogger(__name__)


class _SyncTriggerJsonResource(JsonResource):
    """JsonResource subclass that attempts to start the background sync
    on every incoming request. If the data isn't ready yet, the next API call
    will retry automatically.
    """

    def __init__(self, hs, syncer: GroupMembershipSyncer):
        super().__init__(hs)
        self._syncer = syncer

    def render(self, request):
        response_body = super().render(request)
        self._syncer.start()
        return response_body


class FamedlyControl:
    # NOTE: Adjust the openapi-spec.yaml file version when this changes. Match this even
    # if there are no changes to the openapi spec.
    __version__ = "0.0.2"

    def __init__(self, config: FamedlyControlConfig, api: ModuleApi) -> None:
        self.api = api
        self.server_name = api.server_name
        self.clock = api._hs.get_clock()
        self.config = config
        self.client = FamedlyControlClient(self.api, config)
        self.room_handler = ManagedRoomHandler(self.api, self.config, self.client)
        self.repository = ManagedRoomRepository(api)

        if self.api.should_run_background_tasks():
            self.syncer = GroupMembershipSyncer(
                api, self.client, self.room_handler, self.repository, config
            )

            # Register servlets
            self.resource = _SyncTriggerJsonResource(self.api._hs, self.syncer)
            CreateManagedRoomResource(
                self.api, self.room_handler, self.repository
            ).register(self.resource)
            ListManagedRoomsResource(self.api, self.repository).register(self.resource)
            AssignGroupsToManagedRoomResource(
                self.api, self.room_handler, self.repository
            ).register(self.resource)
            self.api.register_web_resource(MANAGED_ROOM_API_PREFIX, self.resource)

            self.api._clock.call_when_running(self.syncer.start)

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
        # Because of the check for a managed room, this function does not run when
        # creating a room. A room can not be marked as managed until after its creation.
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
        self, event: EventBase, state_events: StateMap[EventBase], room_creator: str
    ) -> tuple[bool, dict | None]:
        """Block invalid power level changes in managed rooms. While any system admin
        can make changes to this event, protect that the levels we consider invariant
        can not be changed. Use the comprehensive Pydantic model built for this purpose.

        Enforced constraints (as a non-exhaustive list):
        * The admin must remain the highest power level in the room.
        * Certain event type power levels are not allowed to be changed
          (``m.room.join_rules``, ``m.room.power_levels``, ``m.room.guest_access``).
        * Membership actions (ban, kick, invite) must not ever be lower than the room
          creator's PL
        """
        new_content = event.content
        try:
            PowerLevelEventContent.model_validate(
                new_content, context={"room_creator": room_creator}
            )
            return True, None
        except ValidationError as e:
            # For returning the error to the client, just select the first error
            err = e.errors()[0]
            single_validation_error = {"loc": err.get("loc"), "msg": err.get("msg")}

            raise SynapseError(
                403,
                f"Invalid request body: {single_validation_error}",
                errcode=Codes.FORBIDDEN,
            )
