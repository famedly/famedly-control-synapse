import logging
import re
from typing import Iterable, Pattern

from pydantic import ValidationError
from synapse.api.constants import CREATOR_POWER_LEVEL
from synapse.api.room_versions import KNOWN_ROOM_VERSIONS
from synapse.http.servlet import (
    RestServlet,
    parse_integer,
    parse_json_object_from_request,
)
from synapse.http.site import SynapseRequest
from synapse.module_api import ModuleApi
from synapse.module_api.errors import Codes
from synapse.types import JsonDict, RoomID

from famedly_control_synapse.client import FamedlyControlError
from famedly_control_synapse.repository import ManagedRoomRepository
from famedly_control_synapse.rest.types import (
    AssignGroupsToManagedRoomRequest,
    CreateManagedRoomRequest,
)
from famedly_control_synapse.room_handler import ManagedRoomHandler

MANAGED_ROOM_API_PREFIX = "/_famedlyControl/v1/managedRooms"
logger = logging.getLogger(__name__)


def famedly_control_patterns(path_regex: str) -> Iterable[Pattern]:
    """Returns the list of patterns for a managed room endpoint

    Args:
        path_regex: The regex string to match. This should NOT have a ^
            as this will be prefixed.

    Returns:
        A list of regex patterns.
    """
    famedly_control_prefix = "^" + MANAGED_ROOM_API_PREFIX
    patterns = [re.compile(famedly_control_prefix + path_regex)]
    return patterns


class CreateManagedRoomResource(RestServlet):
    """Resource for creating a new managed room."""

    PATTERNS = famedly_control_patterns("/createRoom")

    def __init__(
        self,
        api: ModuleApi,
        room_handler: ManagedRoomHandler,
        repository: ManagedRoomRepository,
    ) -> None:
        super().__init__()
        self.api = api
        self.room_handler = room_handler
        self.repository = repository

    async def on_POST(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        """Handle POST requests to create a new managed room."""
        requester = await self.api.get_user_by_req(request)
        admin_user_id = requester.user.to_string()

        if not await self.api.is_user_admin(admin_user_id):
            raise FamedlyControlError(403, "User is not administrator", Codes.FORBIDDEN)

        room_config = parse_json_object_from_request(request)

        try:
            if room_config.get("room_version") is None:
                room_config["room_version"] = (
                    self.api._hs.config.server.default_room_version.identifier
                )
            validated_room_config = CreateManagedRoomRequest.model_validate(
                room_config, context={"room_creator": admin_user_id}
            )
        except ValidationError as e:
            validation_error = [
                {"loc": err.get("loc"), "msg": err.get("msg")} for err in e.errors()
            ]
            raise FamedlyControlError(
                400,
                "Invalid request body",
                errcode=Codes.BAD_JSON,
                additional_fields={"details": validation_error},
            )

        room_version = KNOWN_ROOM_VERSIONS.get(validated_room_config.room_version)
        if room_version is None:
            raise FamedlyControlError(
                400,
                f"Unsupported room version: {validated_room_config.room_version}",
                errcode=Codes.UNSUPPORTED_ROOM_VERSION,
            )

        if not room_version.msc4289_creator_power_enabled:
            validated_room_config.power_level_content_override.users[admin_user_id] = (
                CREATOR_POWER_LEVEL - 1
            )

        room_id, _ = await self.api.create_room(
            admin_user_id,
            validated_room_config.model_dump(by_alias=True, exclude_none=True),
        )

        await self.repository.initialize_sync_token(admin_user_id)

        # if there is a problem, or the members are only partially assigned, this will
        # respond directly
        await self.room_handler.assign_groups_to_room(
            room_id, admin_user_id, validated_room_config.groups
        )

        return 200, {"room_id": room_id, "groups": validated_room_config.groups}


class ListManagedRoomsResource(RestServlet):
    """Resource for listing all managed rooms."""

    PATTERNS = famedly_control_patterns("/rooms")

    def __init__(self, api: ModuleApi, repository: ManagedRoomRepository) -> None:
        super().__init__()
        self.api = api
        self.repo = repository

    async def on_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        """Handle GET requests to list managed rooms."""
        requester = await self.api.get_user_by_req(request)
        user_id = requester.user.to_string()

        if not await self.api.is_user_admin(user_id):
            raise FamedlyControlError(
                403, "User is not administrator", errcode=Codes.FORBIDDEN
            )

        # The 'from' query parameter is labeled as a string in the openapi spec, but is
        # passed directly into the sql query which expects it to be an integer(for
        # OFFSET). Just parse it as the integer directly. This allows it to have a
        # default when one is not supplied, disallows negative numbers, and will raise
        # as a 400 with M_INVALID_PARAM when it is not a legitimate integer. Since the
        # 'from' parameter should have come from a previous page of this endpoint, this
        # should be safe.
        from_token = parse_integer(request, "from", default=0)
        limit = parse_integer(request, "limit", default=100)

        total_count = await self.repo.count_managed_rooms()
        entries = await self.repo.get_managed_rooms_paginated(limit + 1, from_token)

        has_next = len(entries) > limit
        chunk = entries[:limit]

        response: JsonDict = {
            "chunk": chunk,
            "total_room_count_estimate": total_count,
        }

        if has_next:
            response["next_batch"] = str(from_token + limit)
        if from_token > 0:
            response["prev_batch"] = str(max(0, from_token - limit))

        return 200, response


class AssignGroupsToManagedRoomResource(RestServlet):
    """Resource for assigning groups to a managed room."""

    PATTERNS = famedly_control_patterns("/(?P<room_id>[^/]*)/groups")

    def __init__(
        self,
        api: ModuleApi,
        room_handler: ManagedRoomHandler,
        repository: ManagedRoomRepository,
    ) -> None:
        super().__init__()
        self.api = api
        self.room_handler = room_handler
        self.repository = repository

    async def on_POST(
        self, request: SynapseRequest, room_id: str
    ) -> tuple[int, JsonDict]:
        """Handle POST requests to assign groups to a managed room."""
        # Validate room_id format to prevent malicious input
        _ = RoomID.from_string(room_id)

        # Validate user permissions
        requester = await self.api.get_user_by_req(request)
        user_id = requester.user.to_string()
        if not await self.api.is_user_admin(user_id):
            raise FamedlyControlError(
                403, "User is not administrator", errcode=Codes.FORBIDDEN
            )

        # Validate if it's a managed room
        if not await self.repository.is_managed_room(room_id, user_id):
            # TODO: Different errcode here?
            raise FamedlyControlError(
                404, "Room not found or not a managed room", errcode=Codes.NOT_FOUND
            )

        # Updated groups information from the request body
        try:
            validated_input = AssignGroupsToManagedRoomRequest.model_validate(
                parse_json_object_from_request(request)
            )
        except ValidationError as e:
            validation_error = [
                {"loc": err.get("loc"), "msg": err.get("msg")} for err in e.errors()
            ]
            raise FamedlyControlError(
                400,
                "Invalid request body",
                errcode=Codes.BAD_JSON,
                additional_fields={"details": validation_error},
            )

        # if there is a problem, or the members are only partially assigned, this will
        # respond directly
        await self.room_handler.assign_groups_to_room(
            room_id, user_id, validated_input.groups
        )

        return 200, {"room_id": room_id, "groups": validated_input.groups}
