import logging
import re
from collections.abc import Iterable
from re import Pattern

from pydantic import ValidationError
from synapse.api.constants import CREATOR_POWER_LEVEL
from synapse.api.room_versions import KNOWN_ROOM_VERSIONS
from synapse.http.servlet import (
    RestServlet,
    parse_integer,
    parse_json_object_from_request,
    parse_string,
)
from synapse.http.site import SynapseRequest
from synapse.module_api import ModuleApi
from synapse.module_api.errors import Codes
from synapse.types import JsonDict, RoomID

from famedly_control_synapse.client import FamedlyControlError
from famedly_control_synapse.repository import (
    VALID_ORDER_BY_FIELDS,
    ManagedRoomRepository,
)
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


async def assert_famedly_control_admin(
    api: ModuleApi, request: SynapseRequest, admin_user: str
) -> str:
    """Authenticate the request and ensure the caller is the single configured
    Famedly Control admin.

    Args:
        api: The module API used to resolve the requesting user.
        request: The incoming HTTP request.
        admin_user: The configured Famedly Control admin Matrix user ID.

    Returns:
        The requesting user's Matrix user ID (equal to ``admin_user``).

    Raises:
        FamedlyControlError: A 403 if the caller is not the configured admin or is
            not a server administrator.
    """
    requester = await api.get_user_by_req(request)
    user_id = requester.user.to_string()
    if user_id != admin_user or not await api.is_user_admin(user_id):
        raise FamedlyControlError(
            403,
            "Only the configured Famedly Control admin may call this API",
            errcode=Codes.FORBIDDEN,
        )
    return user_id


class CreateManagedRoomResource(RestServlet):
    """Resource for creating a new managed room."""

    PATTERNS = famedly_control_patterns("/createRoom")

    def __init__(
        self,
        api: ModuleApi,
        room_handler: ManagedRoomHandler,
        repository: ManagedRoomRepository,
        admin_user: str,
    ) -> None:
        super().__init__()
        self.api = api
        self.room_handler = room_handler
        self.repository = repository
        self.admin_user = admin_user

    async def on_POST(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        """Handle POST requests to create a new managed room."""
        admin_user_id = await assert_famedly_control_admin(
            self.api, request, self.admin_user
        )

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

        # Fetch the group members *before* creating the room. If this fails (e.g. the
        # Famedly Control API is unreachable), we skip room creation entirely and return
        # an error, rather than leaving behind a partial room the client cannot recover.
        try:
            expected_member_external_ids = await self.room_handler.fetch_group_members(
                validated_room_config.groups
            )
        except FamedlyControlError as e:
            raise FamedlyControlError(
                e.code,
                e.msg,
                errcode=e.errcode,
                additional_fields={"groups": validated_room_config.groups},
            )

        await self.repository.initialize_sync_token(admin_user_id)

        room_id, _ = await self.api.create_room(
            admin_user_id,
            validated_room_config.model_dump(by_alias=True, exclude_none=True),
        )

        # Once the room exists, any failure while assigning groups would leave a partial
        # managed room behind. Delete the room immediately so we never end up in that
        # state, then re-raise the error to the client.
        try:
            await self.room_handler.assign_groups_to_room(
                room_id,
                admin_user_id,
                validated_room_config.groups,
                expected_member_external_ids=expected_member_external_ids,
            )
        except Exception:
            logger.exception(
                "Failed to assign groups to newly created room %s; deleting it to "
                "avoid a partial managed room",
                room_id,
            )
            try:
                await self.api.delete_room(room_id)
            except Exception:
                logger.exception(
                    "Failed to delete room %s during rollback; it may be left partial",
                    room_id,
                )
                raise FamedlyControlError(
                    500,
                    "Failed to create managed room and could not clean up the "
                    "partially created room; manual intervention may be required",
                    errcode=Codes.UNKNOWN,
                    additional_fields={"room_id": room_id},
                )
            # assign_groups_to_room may have queued retry-queue entries for this room
            # before failing. Drop them so the now-deleted room doesn't abort future
            # retry-queue processing. Persist the removal too: the background processor
            # may have already saved a snapshot containing this room, and an in-memory
            # pop alone would leave the deleted room in the on-disk snapshot.
            try:
                async with self.room_handler.retry_queue_lock:
                    if (
                        self.room_handler.retry_queue.rooms.pop(room_id, None)
                        is not None
                    ):
                        await self.room_handler.save_retry_queue_snapshot(admin_user_id)
            except Exception:
                logger.exception(
                    "Failed to clean up retry queue for room %s during rollback",
                    room_id,
                )
            raise

        return 200, {"room_id": room_id, "groups": validated_room_config.groups}


class ListManagedRoomsResource(RestServlet):
    """Resource for listing all managed rooms."""

    PATTERNS = famedly_control_patterns("/rooms")

    def __init__(
        self, api: ModuleApi, repository: ManagedRoomRepository, admin_user: str
    ) -> None:
        super().__init__()
        self.api = api
        self.repo = repository
        self.admin_user = admin_user

    async def on_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        """Handle GET requests to list managed rooms."""
        await assert_famedly_control_admin(self.api, request, self.admin_user)

        # The 'from' query parameter is labeled as a string in the openapi spec, but is
        # passed directly into the sql query which expects it to be an integer(for
        # OFFSET). Just parse it as the integer directly. This allows it to have a
        # default when one is not supplied, disallows negative numbers, and will raise
        # as a 400 with M_INVALID_PARAM when it is not a legitimate integer. Since the
        # 'from' parameter should have come from a previous page of this endpoint, this
        # should be safe.
        from_token = parse_integer(request, "from", default=0)
        limit = parse_integer(request, "limit", default=100)
        search_term = parse_string(request, "search_term")
        managed_room_group_id = parse_string(request, "managed_room_group_id")
        order_by = parse_string(request, "order_by", default="name")
        dir_param = parse_string(request, "dir", default="f")

        if order_by not in VALID_ORDER_BY_FIELDS:
            raise FamedlyControlError(
                400,
                f"Unknown order_by value: {order_by!r}",
                errcode=Codes.INVALID_PARAM,
            )
        if dir_param not in ("f", "b"):
            raise FamedlyControlError(
                400,
                "dir must be 'f' or 'b'",
                errcode=Codes.INVALID_PARAM,
            )
        direction = "ASC" if dir_param == "f" else "DESC"

        total_count = await self.repo.count_managed_rooms(
            search_term, managed_room_group_id
        )
        entries = await self.repo.get_managed_rooms_paginated(
            limit + 1,
            from_token,
            search_term=search_term,
            order_by=order_by,
            direction=direction,
            managed_room_group_id=managed_room_group_id,
        )

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


class GetManagedRoomResource(RestServlet):
    """Resource for fetching a single managed room."""

    # Anchored with '$' so this does not swallow sibling routes such as
    # '/{room_id}/groups'. It still overlaps with the literal '/rooms' route,
    # so this servlet MUST be registered after ListManagedRoomsResource, which
    # Synapse then matches first (routes are checked in registration order).
    PATTERNS = famedly_control_patterns("/(?P<room_id>[^/]*)$")

    def __init__(
        self, api: ModuleApi, repository: ManagedRoomRepository, admin_user: str
    ) -> None:
        super().__init__()
        self.api = api
        self.repo = repository
        self.admin_user = admin_user

    async def on_GET(
        self, request: SynapseRequest, room_id: str
    ) -> tuple[int, JsonDict]:
        """Handle GET requests to fetch a single managed room."""
        await assert_famedly_control_admin(self.api, request, self.admin_user)

        entry = await self.repo.get_managed_room(room_id)
        if entry is None:
            raise FamedlyControlError(
                404, "Room not found or not a managed room", errcode=Codes.NOT_FOUND
            )

        return 200, entry


class AssignGroupsToManagedRoomResource(RestServlet):
    """Resource for assigning groups to a managed room."""

    PATTERNS = famedly_control_patterns("/(?P<room_id>[^/]*)/groups")

    def __init__(
        self,
        api: ModuleApi,
        room_handler: ManagedRoomHandler,
        repository: ManagedRoomRepository,
        admin_user: str,
    ) -> None:
        super().__init__()
        self.api = api
        self.room_handler = room_handler
        self.repository = repository
        self.admin_user = admin_user

    async def on_POST(
        self, request: SynapseRequest, room_id: str
    ) -> tuple[int, JsonDict]:
        """Handle POST requests to assign groups to a managed room."""
        # Validate room_id format to prevent malicious input
        _ = RoomID.from_string(room_id)

        # Validate user permissions
        user_id = await assert_famedly_control_admin(self.api, request, self.admin_user)

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
