from pydantic import ValidationError
from synapse.http.server import DirectServeJsonResource
from synapse.http.servlet import parse_json_object_from_request
from synapse.http.site import SynapseRequest
from synapse.module_api import ModuleApi
from synapse.types import JsonDict

from famedly_control_synapse.config import FamedlyControlConfig
from famedly_control_synapse.types import CreateManagedRoomRequest

MANAGED_ROOM_API_PREFIX = "/_famedlyControl/v1/managedRooms"
MANAGED_ROOM_TYPE = "de.famedly.managedRoom"


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
