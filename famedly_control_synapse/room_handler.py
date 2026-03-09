import logging

from synapse.api.constants import EventTypes
from synapse.module_api import ModuleApi
from synapse.types import Requester, create_requester

from famedly_control_synapse.config import FamedlyControlConfig

logger = logging.getLogger(__name__)

CREATE_EVENT_FILTER = (EventTypes.Create, "")


class ManagedRoomHandler:
    def __init__(self, api: ModuleApi, config: FamedlyControlConfig):
        self.api = api
        self.config = config

    async def force_join_users_to_room(
        self, room_id: str, user_mxids: list[str], requester: Requester
    ) -> None | dict[str, str]:
        """Force join users to a managed room that is invite-only.

        Args:
            room_id: The ID of the room to join.
            user_mxids: The list of Matrix user IDs to join.
            requester: The requester who is the admin/room creator performing the action.
        """

        errors = {}
        for member in user_mxids:
            try:
                fake_requester = create_requester(
                    member, authenticated_entity=requester.authenticated_entity
                )
                # First invite the user, managed room is invite-only.
                await self.api._hs.get_room_member_handler().update_membership(
                    requester=requester,
                    target=fake_requester.user,
                    room_id=room_id,
                    action="invite",
                    remote_room_hosts=None,
                    ratelimit=False,
                )
                # Make sure that the user force joins the room
                await self.api._hs.get_room_member_handler().update_membership(
                    requester=fake_requester,
                    target=fake_requester.user,
                    room_id=room_id,
                    action="join",
                    remote_room_hosts=None,
                    ratelimit=False,
                )
            except Exception as e:
                error_msg = str(e)
                # Skip users who are already in the room
                if "is already in the room" in error_msg:
                    logger.info("Skipping %s: already in room %s", member, room_id)
                    # TODO introduce metric to check how often this is happening
                    continue
                errors[member] = error_msg
                logger.exception(
                    "Failed to update room membership for %s: %s", member, e
                )
        if errors:
            return errors
        return None

    async def remove_users_from_room(
        self, creator_id: str, user_mxids: list[str], room_id: str
    ) -> None | dict[str, str]:
        errors = {}
        for member in user_mxids:
            try:
                await self.api.update_room_membership(
                    sender=creator_id,
                    target=member,
                    room_id=room_id,
                    new_membership="leave",
                    content={"reason": "Group has been removed from the room"},
                )
            except Exception as e:
                logger.warning(
                    "Failed to remove user %s from room %s: %s", member, room_id, e
                )
                errors[member] = str(e)
        if errors:
            return errors
        return None

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
