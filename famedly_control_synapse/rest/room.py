import re
from typing import Iterable, Pattern

from pydantic import ValidationError
from synapse.api.constants import CREATOR_POWER_LEVEL
from synapse.api.errors import SynapseError
from synapse.api.room_versions import KNOWN_ROOM_VERSIONS
from synapse.http.servlet import (
    RestServlet,
    parse_integer,
    parse_json_object_from_request,
    parse_string,
)
from synapse.http.site import SynapseRequest
from synapse.module_api import ModuleApi
from synapse.types import JsonDict, RoomID

from famedly_control_synapse.client import FamedlyControlClient
from famedly_control_synapse.repository import ManagedRoomRepository
from famedly_control_synapse.room_handler import ManagedRoomHandler
from famedly_control_synapse.types import (
    MANAGED_ROOM_TYPE,
    AssignGroupsToManagedRoomRequest,
    CreateManagedRoomRequest,
)

MANAGED_ROOM_API_PREFIX = "/_famedlyControl/v1/managedRooms"


def famedly_control_patterns(path_regex: str) -> Iterable[Pattern]:
    """Returns the list of patterns for a managed room endpoint

    Args:
        path_regex: The regex string to match. This should NOT have a ^
            as this will be prefixed.

    Returns:
        A list of regex patterns.
    """
    famedly_control_prefix = "^/_famedlyControl/v1/managedRooms"
    patterns = [re.compile(famedly_control_prefix + path_regex)]
    return patterns


class CreateManagedRoomResource(RestServlet):
    """Resource for creating a new managed room."""

    PATTERNS = famedly_control_patterns("/createRoom")

    def __init__(
        self,
        api: ModuleApi,
        client: FamedlyControlClient,
        room_handler: ManagedRoomHandler,
    ) -> None:
        super().__init__()
        self.api = api
        self.client = client
        self.account_data_handler = self.api._account_data_handler
        self.room_handler = room_handler

    async def on_POST(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        """Handle POST requests to create a new managed room."""
        requester = await self.api.get_user_by_req(request)
        admin_user_id = requester.user.to_string()

        if not await self.api.is_user_admin(admin_user_id):
            return 403, {"error": "user is not administrator"}

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
                    admin_user_id: CREATOR_POWER_LEVEL - 1
                }

        room_id, _ = await self.api.create_room(
            admin_user_id,
            validated_room_config.model_dump(by_alias=True, exclude_none=True),
        )

        await self.account_data_handler.add_account_data_to_room(
            admin_user_id,
            room_id,
            MANAGED_ROOM_TYPE,
            {"groups": validated_room_config.groups},
        )

        members = []
        for group_id in validated_room_config.groups:
            group_members = await self.client.get_group_members(group_id)
            members.extend(group_members)
        await self.room_handler.force_join_users_to_room(room_id, members, requester)

        return 200, {"room_id": room_id}


class ListManagedRoomsResource(RestServlet):
    """Resource for listing all managed rooms."""

    PATTERNS = famedly_control_patterns("/rooms")

    def __init__(self, api: ModuleApi) -> None:
        super().__init__()
        self.api = api

    async def on_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
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


class AssignGroupsToManagedRoomResource(RestServlet):
    """Resource for assigning groups to a managed room."""

    PATTERNS = famedly_control_patterns("/(?P<room_id>[^/]*)/groups")

    def __init__(
        self,
        api: ModuleApi,
        client: FamedlyControlClient,
        room_handler: ManagedRoomHandler,
    ) -> None:
        super().__init__()
        self.api = api
        self.client = client
        self.account_data_handler = self.api._account_data_handler
        self.room_handler = room_handler

    async def on_POST(
        self, request: SynapseRequest, room_id: str
    ) -> tuple[int, JsonDict]:
        """Handle POST requests to assign groups to a managed room."""
        try:
            # Validate room_id format to prevent malicious input
            RoomID.from_string(room_id)
        except SynapseError:
            # Return error resource for invalid room IDs
            return 400, {"error": "Invalid room ID format"}

        requester = await self.api.get_user_by_req(request)
        user_id = requester.user.to_string()
        if not await self.api.is_user_admin(user_id):
            return 403, {"error": "user is not administrator"}

        # Get the original groups from room account data
        old_groups_info = await self.api._store.get_account_data_for_room_and_type(
            user_id, room_id, MANAGED_ROOM_TYPE
        )
        old_groups = old_groups_info.get("groups", []) if old_groups_info else []

        # Get the individuals of the old group
        old_members = set()
        for group_id in old_groups:
            members = await self.client.get_group_members(group_id)
            old_members.update(members)

        # New groups to be added
        try:
            validated_input = AssignGroupsToManagedRoomRequest.model_validate(
                parse_json_object_from_request(request)
            )
        except ValidationError as e:
            errors = [
                {"loc": err.get("loc"), "msg": err.get("msg")} for err in e.errors()
            ]
            return 400, {"error": "Invalid request body", "details": errors}

        new_members = set()
        for group_id in validated_input.groups:
            members = await self.client.get_group_members(group_id)
            new_members.update(members)

        # Calculate who to remove and who to add
        members_to_remove = old_members - new_members
        members_to_add = new_members - old_members

        # Kick out members who are no longer in any group
        for member in members_to_remove:
            await self.api.update_room_membership(
                sender=user_id,
                target=member,
                room_id=room_id,
                new_membership="leave",
                content={"reason": "Group has been removed from the room"},
            )

        # Add new members who weren't in the old groups
        await self.room_handler.force_join_users_to_room(
            room_id, list(members_to_add), requester
        )

        # Update room account data with new groups information
        await self.account_data_handler.remove_account_data_for_room(
            user_id, room_id, MANAGED_ROOM_TYPE
        )
        await self.account_data_handler.add_account_data_to_room(
            user_id,
            room_id,
            MANAGED_ROOM_TYPE,
            {"groups": validated_input.groups},
        )
        return 200, {"room_id": room_id, "groups": validated_input.groups}
