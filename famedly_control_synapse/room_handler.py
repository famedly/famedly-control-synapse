import logging

from synapse.module_api import ModuleApi
from synapse.types import Requester, create_requester


class ManagedRoomHandler:
    def __init__(self, api: ModuleApi):
        self.api = api

    async def force_join_users_to_room(
        self, room_id: str, users: list[str], requester: Requester
    ) -> None:
        """Force join users to a managed room that is invite-only.

        Args:
            room_id: The ID of the room to join.
            users: The list of user IDs to join.
            requester: The requester who is the admin/room creator performing the action.
        """
        for member in users:
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
                logging.exception(
                    "Failed to update room membership for %s: %s", member, e
                )
