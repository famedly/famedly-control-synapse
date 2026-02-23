from pydantic import ValidationError
from synapse.api.constants import CREATOR_POWER_LEVEL
from synapse.api.room_versions import KNOWN_ROOM_VERSIONS
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
from famedly_control_synapse.repository import ManagedRoomRepository
from famedly_control_synapse.types import MANAGED_ROOM_TYPE, CreateManagedRoomRequest

MANAGED_ROOM_API_PREFIX = "/_famedlyControl/v1/managedRooms"


class RoomIdRouter(DirectServeJsonResource):
    """Routes requests with room_id path variable to the appropriate resource."""

    def __init__(self, api: ModuleApi, config: FamedlyControlConfig):
        DirectServeJsonResource.__init__(self)
        self.api = api
        self.config = config
        self.isLeaf = False

    def getChild(self, path: bytes, request):
        """Handle /{room_id}/... pattern."""
        room_id = path.decode("utf-8")
        room_resource = DirectServeJsonResource()
        room_resource.putChild(
            b"groups", AssignGroupToManagedRoomResource(self.api, self.config, room_id)
        )
        return room_resource


class ManagedRoomResource(DirectServeJsonResource):
    def __init__(
        self,
        api: ModuleApi,
        config: FamedlyControlConfig,
    ) -> None:
        super().__init__()
        self.api = api
        self.config = config
        self.account_data_handler = self.api._hs.get_account_data_handler()

    async def _assert_requester_is_admin(
        self, request: SynapseRequest
    ) -> tuple[str | None, int | None, JsonDict | None]:
        """Check if requester is an admin. Returns user_id if admin, or raises an error if not.

        Args:
            request: The request to check.

        Returns:
            Tuple of (user_id, None) if admin, or (None, error_response) if not.
        """
        requester = await self.api.get_user_by_req(request)
        user_id = requester.user.to_string()

        if not await self.api.is_user_admin(user_id):
            return None, 403, {"error": "user is not administrator"}

        return user_id, None, None


class CreateManagedRoomResource(ManagedRoomResource):
    def __init__(
        self,
        api: ModuleApi,
        config: FamedlyControlConfig,
    ) -> None:
        super().__init__(api, config)

    async def _async_render_POST(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        """Handle POST requests to create a new managed room."""
        user_id, error_code, error_response = await self._assert_requester_is_admin(
            request
        )
        if error_code:
            return error_code, error_response

        room_config = parse_json_object_from_request(request)

        try:
            validated_room_config = CreateManagedRoomRequest.model_validate(room_config)
        except ValidationError as e:
            errors = [
                {"loc": err.get("loc"), "msg": err.get("msg")} for err in e.errors()
            ]
            return 400, {"error": "Invalid request body", "details": errors}

        room_version = KNOWN_ROOM_VERSIONS.get(validated_room_config.room_version)
        if room_version is None:
            return 400, {
                "error": f"Unsupported room version: {validated_room_config.room_version}"
            }

        if not room_version.msc4289_creator_power_enabled:
            if "power_level_content_override" in room_config:
                validated_room_config.power_level_content_override.users = {
                    user_id: CREATOR_POWER_LEVEL - 1
                }

        room_id, _ = await self.api.create_room(
            user_id, validated_room_config.model_dump(by_alias=True, exclude_none=True)
        )

        await self.account_data_handler.add_account_data_to_room(
            user_id,
            room_id,
            MANAGED_ROOM_TYPE,
            {"groups": validated_room_config.groups},
        )

        return 200, {"room_id": room_id}


class AssignGroupToManagedRoomResource(ManagedRoomResource):
    def __init__(
        self,
        api: ModuleApi,
        config: FamedlyControlConfig,
        room_id: str,
    ) -> None:
        super().__init__(api, config)
        self.room_id = room_id

    async def _async_render_POST(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        """Handle POST requests to assign groups to a managed room."""
        user_id, error_code, error_response = await self._assert_requester_is_admin(
            request
        )
        if error_code:
            return error_code, error_response

        groups = parse_json_object_from_request(request).get("groups", [])

        await self.account_data_handler.remove_account_data_for_room(
            user_id, self.room_id, MANAGED_ROOM_TYPE
        )
        await self.account_data_handler.add_account_data_to_room(
            user_id,
            self.room_id,
            MANAGED_ROOM_TYPE,
            {"groups": groups},
        )
        return 200, {"room_id": self.room_id, "groups": groups}


class ListManagedRoomsResource(ManagedRoomResource):
    async def _async_render_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        """Handle GET requests to list managed rooms."""
        _, error_code, error_response = await self._assert_requester_is_admin(request)
        if error_code:
            return error_code, error_response

        from_token = parse_string(request, "from")
        limit = parse_integer(request, "limit", default=100)

        start_index = 0
        if from_token is not None:
            try:
                start_index = int(from_token)
            except ValueError:
                return 400, {"error": "invalid 'from' parameter"}

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
