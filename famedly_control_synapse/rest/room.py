import logging
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
)
from synapse.http.site import SynapseRequest
from synapse.module_api import ModuleApi
from synapse.types import JsonDict, RoomID

from famedly_control_synapse.client import FamedlyControlClient, Membership
from famedly_control_synapse.repository import ManagedRoomRepository
from famedly_control_synapse.room_handler import ManagedRoomHandler
from famedly_control_synapse.types import (
    MANAGED_ROOM_TYPE,
    AssignGroupsToManagedRoomRequest,
    CreateManagedRoomRequest,
)

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
            validation_error = [
                {"loc": err.get("loc"), "msg": err.get("msg")} for err in e.errors()
            ]
            return 400, {"error": "Invalid request body", "details": validation_error}

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

        member_external_ids = set()
        for group_id in validated_room_config.groups:
            group_diff = await self.client.get_group_members(group_id)
            member_external_ids.update(group_diff)

        join_errors = await self.room_handler.force_join_users_to_room(
            room_id, list(member_external_ids), requester
        )
        if join_errors:
            logger.warning(
                "Some members failed to join room %s: %s", room_id, join_errors
            )
            return 207, {
                "error": "Failed to add some members to the room",
                "details": join_errors,
            }

        return 200, {"room_id": room_id}


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
            return 403, {"error": "user is not administrator"}

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
        # Validate room_id format to prevent malicious input
        try:
            RoomID.from_string(room_id)
            # TODO: add case the room does not exists
            # TODO: add case room that isn't a managed room
        except SynapseError:
            return 400, {"error": "Invalid room ID format. Expected a Matrix room ID."}

        requester = await self.api.get_user_by_req(request)
        user_id = requester.user.to_string()
        if not await self.api.is_user_admin(user_id):
            return 403, {"error": "user is not administrator"}

        # Get the current groups information
        old_groups_info = await self.api._store.get_account_data_for_room_and_type(
            user_id, room_id, MANAGED_ROOM_TYPE
        )
        old_groups = old_groups_info.get("groups", []) if old_groups_info else []

        # New groups information from the request body
        try:
            validated_input = AssignGroupsToManagedRoomRequest.model_validate(
                parse_json_object_from_request(request)
            )
        except ValidationError as e:
            validation_error = [
                {"loc": err.get("loc"), "msg": err.get("msg")} for err in e.errors()
            ]
            return 400, {"error": "Invalid request body", "details": validation_error}

        # TODO: This approach will change again after the API design is finalized.
        # There will be a separate endpoint for just fetching current group members
        # The diff endpoint will be used for periodic sync instead of this on-demand approach.

        # 1. Define the remaining groups, new groups, and removed groups
        remaining_groups = set(old_groups) & set(validated_input.groups)
        new_groups = set(validated_input.groups) - remaining_groups
        removed_groups = set(old_groups) - remaining_groups

        existing_members_external_ids = set()
        members_to_add_external_ids: set[str] = set()
        members_to_remove_external_ids: set[str] = set()

        # 2. For the remaining groups, get the diff and skip all existing ADD members, remove all the REM members.
        # TODO consider the case where there are newly added memebers
        for group_id in remaining_groups:
            # TODO: figure out the sync handling for each group.
            members = await self.client.get_group_members(group_id)
            existing_members_external_ids.update(members)
            group_diff = await self.client.get_group_diff(
                group_id, sync="something", timeout=30
            )
            members_to_add_external_ids.update(
                record.user_id
                for record in group_diff.data
                if record.action == Membership.ADD
            )
            members_to_remove_external_ids.update(
                record.user_id
                for record in group_diff.data
                if record.action == Membership.REM
            )
        # 3. For the new groups, add all the ADD members, skip the removed
        for group_id in new_groups:
            members = await self.client.get_group_members(group_id)
            members_to_add_external_ids.update(members)
        members_to_add_external_ids -= existing_members_external_ids

        # 4. For the removed groups, fetch all and kick them out.
        for group_id in removed_groups:
            members = await self.client.get_group_members(group_id)
            members_to_remove_external_ids.update(members)

        # Kick out members who are no longer in the group
        # TODO: prevent kicking out the room creator
        leave_errors = await self.room_handler.remove_users_from_room(
            user_id, list(members_to_remove_external_ids), room_id
        )
        if leave_errors:
            logger.warning(
                "Some members failed to leave room %s: %s", room_id, leave_errors
            )
            return 207, {
                "error": "Failed to remove some members from the room",
                "details": leave_errors,
            }

        # Add new members
        join_errors = await self.room_handler.force_join_users_to_room(
            room_id, list(members_to_add_external_ids), requester
        )
        if join_errors:
            logger.warning(
                "Some members failed to join room %s: %s", room_id, join_errors
            )
            return 207, {
                "error": "Failed to add some members to the room",
                "details": join_errors,
            }

        # Update room account data with new groups information
        await self.update_room_account_data(user_id, room_id, validated_input.groups)
        return 200, {"room_id": room_id, "groups": validated_input.groups}

    async def update_room_account_data(
        self, user_id: str, room_id: str, groups: list[str]
    ) -> None:
        """Helper method to update the room account data for a user."""
        try:
            await self.account_data_handler.remove_account_data_for_room(
                user_id, room_id, MANAGED_ROOM_TYPE
            )
            await self.account_data_handler.add_account_data_to_room(
                user_id,
                room_id,
                MANAGED_ROOM_TYPE,
                {"groups": groups},
            )
        except Exception as e:
            logger.exception(
                "Failed to update account data for room %s: %s", room_id, e
            )


# TODO: proper error and exception handling
