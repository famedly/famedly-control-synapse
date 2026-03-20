import logging
from dataclasses import dataclass, field

from prometheus_client import Counter
from synapse.api.constants import EventContentFields, EventTypes, Membership
from synapse.api.errors import Codes, UnstableSpecAuthError
from synapse.module_api import ModuleApi
from synapse.types import Requester, create_requester

from famedly_control_synapse.config import FamedlyControlConfig

logger = logging.getLogger(__name__)

CREATE_EVENT_FILTER = (EventTypes.Create, "")

famedly_control_user_sync_error = Counter(
    "famedly_control_user_sync_error",
    "Counts failures when updating user membership in managed rooms. `error_code` is "
    "the Synapse errcode derived from the exception, and `server_name` is the local "
    "homeserver where the error occurred.",
    labelnames=["error_code", "server_name"],
)


@dataclass
class MembershipChangeResult:
    """Result of applying membership changes to a room."""

    not_found_ids: list[str] = field(default_factory=list)
    join_errors: dict[str, str] = field(default_factory=dict)
    leave_errors: dict[str, str] = field(default_factory=dict)

    @property
    def has_errors(self) -> bool:
        return bool(self.not_found_ids or self.join_errors or self.leave_errors)


class ManagedRoomHandler:
    def __init__(self, api: ModuleApi, config: FamedlyControlConfig):
        self.api = api
        self.config = config
        self.server_name = api.server_name

    def increment_error_count(self, error_code: str) -> None:
        """Helper function to increment the user sync error metric with server name."""
        famedly_control_user_sync_error.labels(
            error_code=error_code, server_name=self.server_name
        ).inc()

    def get_error_code_from_exception(self, e: Exception) -> str:
        """Extract error code from an exception, handling different exception types.

        For SynapseError (or other exceptions exposing an ``errcode`` attribute) return
        the Matrix errcode value (e.g., `M_FORBIDDEN` for Codes.FORBIDDEN). For any
        other cases, return `M_UNKNOWN`(Codes.UNKNOWN).
        """
        if hasattr(e, "errcode") and e.errcode and isinstance(e.errcode, Codes):
            return str(e.errcode.value)
        return "M_UNKNOWN"

    async def get_users_room_membership(self, room_id: str, mxid: str) -> str:
        state_key = (EventTypes.Member, mxid)
        state_map = await self.api.get_room_state(room_id, (state_key,))
        membership_event = state_map.get(state_key)
        if membership_event:
            return membership_event.content[EventContentFields.MEMBERSHIP]
        # Default for a room with the member having never been there is "leave"
        return "leave"

    async def force_join_users_to_room(
        self, room_id: str, user_mxids: list[str], requester: Requester
    ) -> dict[str, str]:
        """Force join users to a managed room that is invite-only.

        Args:
            room_id: The ID of the room to join.
            user_mxids: The list of Matrix user IDs to join.
            requester: The requester who is the admin/room creator performing the action.

        Returns: A dict containing any errors keyed by user. Can be empty
        """
        errors = {}
        for member in user_mxids:
            try:
                existing_member = await self.api.check_user_exists(member)
                if existing_member is None:
                    errors[member] = "User does not exist on this server"
                    logger.error("User %s does not exist, skipping", member)
                    self.increment_error_count(error_code=Codes.NOT_FOUND.value)
                    continue

                fake_requester = create_requester(
                    existing_member, authenticated_entity=requester.authenticated_entity
                )
                membership_in_room = await self.get_users_room_membership(
                    room_id, member
                )
                # The member may already be invited or joined to the room. If either,
                # skip the invite
                if membership_in_room not in (Membership.JOIN, Membership.INVITE):

                    # First invite the user, managed room is invite-only.
                    await self.api._hs.get_room_member_handler().update_membership(
                        requester=requester,
                        target=fake_requester.user,
                        room_id=room_id,
                        action=Membership.INVITE,
                        remote_room_hosts=None,
                        ratelimit=False,
                    )

                # Just like above, if the member is already in the room skip it
                if membership_in_room != Membership.JOIN:
                    # Make sure that the user force joins the room
                    await self.api._hs.get_room_member_handler().update_membership(
                        requester=fake_requester,
                        target=fake_requester.user,
                        room_id=room_id,
                        action=Membership.JOIN,
                        remote_room_hosts=None,
                        ratelimit=False,
                    )
            except UnstableSpecAuthError as e:
                # UnstableSpecAuthError uses org.matrix.msc3848.unstable.errcode
                # instead of the standard errcode field in the JSON response.
                # When a user is already joined and we try to invite them again,
                # this error is raised with errcode ALREADY_JOINED.
                if e.errcode == Codes.ALREADY_JOINED:
                    logger.info("Skipping %s: already in room %s", member, room_id)
                    continue
                # For other UnstableSpecAuthError codes, log and continue processing other users
                errors[member] = e.msg
                logger.error("Failed to update room membership for %s: %s", member, e)
                self.increment_error_count(
                    error_code=self.get_error_code_from_exception(e)
                )
            except Exception as e:
                # Catch any other unexpected exceptions
                error_msg = str(e)
                errors[member] = error_msg
                logger.error("Failed to update room membership for %s: %s", member, e)
                self.increment_error_count(
                    error_code=self.get_error_code_from_exception(e)
                )
        return errors

    async def remove_users_from_room(
        self, creator_id: str, user_mxids: list[str], room_id: str
    ) -> dict[str, str]:
        """Force remove users from a managed room.

        Args:
            creator_id: The ID of the room's creator, for creating the leave event
            room_id: The ID of the room.
            user_mxids: The list of Matrix user IDs to kick.

        Returns: A dict containing any errors keyed by user. Can be empty
        """
        errors = {}
        for member in user_mxids:
            try:
                existing_member = await self.api.check_user_exists(member)
                if existing_member is None:
                    errors[member] = "User does not exist on this server"
                    logger.error("User %s does not exist, skipping removal", member)
                    self.increment_error_count(error_code=Codes.NOT_FOUND.value)
                    continue
                if (
                    await self.get_users_room_membership(room_id, member)
                    == Membership.LEAVE
                ):
                    # This user is already in the room, skip it
                    continue

                await self.api.update_room_membership(
                    sender=creator_id,
                    target=existing_member,
                    room_id=room_id,
                    new_membership=Membership.LEAVE,
                    content={"reason": "User has been removed from the room"},
                )
            except Exception as e:
                logger.error(
                    "Failed to remove user %s from room %s: %s", member, room_id, e
                )
                errors[member] = str(e)
                self.increment_error_count(
                    error_code=self.get_error_code_from_exception(e)
                )

        return errors

    async def apply_membership_changes(
        self,
        room_id: str,
        admin_user_id: str,
        mxids_to_add: list[str],
        mxids_to_remove: list[str],
    ) -> MembershipChangeResult:
        """Apply membership changes to a room using Matrix user IDs.

        Args:
            room_id: The room to modify.
            admin_user_id: The admin user performing the changes.
            mxids_to_add: Matrix user IDs to invite/join. Can be empty list
            mxids_to_remove: Matrix user IDs to remove. Can be empty list

        Returns:
            A result indicating which operations failed, if any.
        """
        requester = create_requester(admin_user_id)
        join_errors = await self.force_join_users_to_room(
            room_id, mxids_to_add, requester
        )

        leave_errors = await self.remove_users_from_room(
            admin_user_id, mxids_to_remove, room_id
        )

        return MembershipChangeResult(
            join_errors=join_errors, leave_errors=leave_errors
        )

    async def apply_membership_changes_from_external_ids(
        self,
        room_id: str,
        admin_user_id: str,
        external_ids_to_add: list[str],
        external_ids_to_remove: list[str],
    ) -> MembershipChangeResult:
        """Convert external user IDs and apply membership changes to a room.

        Args:
            room_id: The room to modify.
            admin_user_id: The admin user performing the changes.
            external_ids_to_add: External user IDs to invite/join. Can be empty list
            external_ids_to_remove: External user IDs to remove. Can be empty list

        Returns:
            A result indicating which operations failed, if any.
        """
        (
            mxids_to_add,
            adds_not_found,
        ) = await self.batch_convert_external_user_ids_to_matrix_user_ids(
            external_ids_to_add
        )

        (
            mxids_to_remove,
            removes_not_found,
        ) = await self.batch_convert_external_user_ids_to_matrix_user_ids(
            external_ids_to_remove
        )

        inner = await self.apply_membership_changes(
            room_id, admin_user_id, mxids_to_add, mxids_to_remove
        )
        return MembershipChangeResult(
            not_found_ids=adds_not_found + removes_not_found,
            join_errors=inner.join_errors,
            leave_errors=inner.leave_errors,
        )

    async def batch_convert_external_user_ids_to_matrix_user_ids(
        self, external_user_ids: list[str]
    ) -> tuple[list[str], list[str]]:
        """Convert multiple external user IDs to Matrix user IDs in a single database query.

        Args:
            external_user_ids: List of external user IDs to convert.

        Returns:
            Tuple of (found Matrix user IDs, not found external user IDs).
        """
        if not external_user_ids:
            return [], []

        rows = await self.api._store.db_pool.simple_select_many_batch(
            table="user_external_ids",
            column="external_id",
            iterable=external_user_ids,
            keyvalues={"auth_provider": self.config.auth_provider},
            retcols=["external_id", "user_id"],
            desc="batch_get_user_by_external_id",
            batch_size=100,
        )

        external_to_matrix = {row[0]: row[1] for row in rows} if rows else {}
        result = []
        not_found_ids = []
        for external_id in external_user_ids:
            if external_id in external_to_matrix:
                result.append(external_to_matrix[external_id])
            else:
                not_found_ids.append(external_id)
        if not_found_ids:
            logger.warning(
                "The following external user IDs from '%s' were not found: %s",
                self.config.auth_provider,
                not_found_ids,
            )

        return result, not_found_ids

    async def get_room_creator(self, room_id: str) -> str | None:
        """Get the room creator from the m.room.create event.

        Args:
            room_id: The room ID to get the creator for.

        Returns:
            The Matrix user ID of the room creator, or None if not found.
        """
        try:
            state_map = await self.api.get_room_state(
                room_id=room_id, event_filter=[CREATE_EVENT_FILTER]
            )
            create_event = state_map.get(CREATE_EVENT_FILTER)
            if create_event:
                # Room versions ≤10 have explicit creator field
                creator_from_content = create_event.content.get("creator")
                # Room versions 11+ use sender
                sender = create_event.sender
                return creator_from_content or sender
        except Exception as e:
            logger.warning("Failed to get room creator for %s: %s", room_id, e)
        return None
