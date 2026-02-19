from pydantic import ValidationError
from synapse.http.server import DirectServeJsonResource
from synapse.http.servlet import (
    parse_integer,
    parse_json_object_from_request,
    parse_string,
)
from synapse.http.site import SynapseRequest
from synapse.module_api import ModuleApi
from synapse.types import JsonDict

from famedly_control_synapse.config import FamedlyControlConfig
from famedly_control_synapse.types import MANAGED_ROOM_TYPE, CreateManagedRoomRequest

MANAGED_ROOM_API_PREFIX = "/_famedlyControl/v1/managedRooms"


class ManagedRoomResource(DirectServeJsonResource):
    def __init__(
        self,
        api: ModuleApi,
        config: FamedlyControlConfig,
    ) -> None:
        super().__init__(api, config)
        self.api = api
        self.config = config
        self.account_data_handler = self.api._hs.get_account_data_handler()


class CreateManagedRoomResource(ManagedRoomResource):
    def __init__(
        self,
        api: ModuleApi,
        config: FamedlyControlConfig,
    ) -> None:
        super().__init__(api, config)
        self.api = api
        self.config = config
        self.account_data_handler = self.api._hs.get_account_data_handler()

    async def _async_render_POST(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        """Handle POST requests to create a new managed room."""
        requester = await self.api.get_user_by_req(request)
        user_id = requester.user.to_string()
        room_config = parse_json_object_from_request(request)

        try:
            validated_room_config = CreateManagedRoomRequest.model_validate(room_config)
        except ValidationError as e:
            return 400, {"error": f"Invalid request body: {e}"}

        if (
            validated_room_config.room_version
            and int(validated_room_config.room_version) < 12
        ):
            if validated_room_config.power_level_content_override:
                validated_room_config.power_level_content_override.users = {
                    user_id: 100
                }

        room_id, _ = await self.api.create_room(
            user_id, validated_room_config.model_dump()
        )

        await self.account_data_handler.add_account_data_to_room(
            user_id,
            room_id,
            MANAGED_ROOM_TYPE,
            {"groups": validated_room_config.groups or []},
        )

        return 200, {"room_id": room_id}


class ListManagedRoomsResource(ManagedRoomResource):
    async def _async_render_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        """Handle GET requests to list managed rooms."""
        requester = await self.api.get_user_by_req(request)
        user_id = requester.user.to_string()

        if not await self.api.is_user_admin(user_id):
            return 403, {"error": "user is not administrator"}

        from_token = parse_string(request, "from")
        limit = parse_integer(request, "limit", default=100)

        start_index = 0
        if from_token is not None:
            try:
                start_index = int(from_token)
            except ValueError:
                return 400, {"error": "invalid 'from' parameter"}

        from famedly_control_synapse.repository import ManagedRoomRepository

        repository = ManagedRoomRepository(self.api)
        total_count = await repository.count_managed_rooms()
        entries = await repository.get_managed_rooms_paginated(limit + 1, start_index)

        has_next = len(entries) > limit
        chunk = entries[:limit]

        response: JsonDict = {
            "chunk": chunk,
            "total_room_count_estimate": total_count,
        }

        if has_next:
            response["next_batch"] = str(start_index + limit)
        if start_index > 0:
            response["prev_batch"] = str(max(0, start_index - limit))

        return 200, response
