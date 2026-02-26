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

from synapse.http.server import JsonResource
from synapse.module_api import ModuleApi

from famedly_control_synapse.client import FamedlyControlClient
from famedly_control_synapse.config import FamedlyControlConfig
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
        self.room_handler = ManagedRoomHandler(self.api)
        # Register REST endpoints with the homeserver
        self.resource = JsonResource(self.api._hs)

        CreateManagedRoomResource(self.api, self.client, self.room_handler).register(
            self.resource
        )
        ListManagedRoomsResource(self.api).register(self.resource)
        AssignGroupsToManagedRoomResource(
            self.api, self.client, self.room_handler
        ).register(self.resource)

        self.api.register_web_resource(MANAGED_ROOM_API_PREFIX, self.resource)
        logger.info("Module initialized")

    @staticmethod
    def parse_config(config: dict[str, Any]) -> FamedlyControlConfig:
        return FamedlyControlConfig.model_validate(config)


# TODO: configure necessary callbacks if needed
