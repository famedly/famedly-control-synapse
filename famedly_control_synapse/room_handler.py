import logging

from synapse.module_api import ModuleApi
from synapse.types import Requester, create_requester

from famedly_control_synapse.config import FamedlyControlConfig

logger = logging.getLogger(__name__)


class ManagedRoomHandler:
    def __init__(self, api: ModuleApi, config: FamedlyControlConfig):
        self.api = api
        self.config = config

    async def force_join_users_to_room(
        self, room_id: str, external_user_ids: list[str], requester: Requester
    ) -> None | dict[str, str]:
        """Force join users to a managed room that is invite-only.

        Args:
            room_id: The ID of the room to join.
            external_user_ids: The list of external user IDs to join.
            requester: The requester who is the admin/room creator performing the action.
        """
        matrix_user_ids = await self.batch_convert_external_user_ids_to_matrix_user_ids(
            external_user_ids
        )
        errors = {}
        for member in matrix_user_ids:
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
                errors[member] = str(e)
                logger.exception(
                    "Failed to update room membership for %s: %s", member, e
                )
        if errors:
            return errors
        return None

    async def remove_users_from_room(
        self, creator_id, external_user_ids, room_id
    ) -> None | dict[str, str]:
        matrix_user_ids = await self.batch_convert_external_user_ids_to_matrix_user_ids(
            external_user_ids
        )
        errors = {}
        for member in matrix_user_ids:
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
    ) -> list[str]:
        """Convert multiple external user IDs to Matrix user IDs in a single database query.

        Args:
            external_user_ids: List of external user IDs to convert.

        Returns:
            List of Matrix user IDs corresponding to the provided external user IDs.
        """
        if not external_user_ids:
            return []

        def _batch_get_users_txn(txn):
            # TODO: Not sure how long the list would be, there might be limitations on amount of the paramters we can pass
            ids = ",".join("?" * len(external_user_ids))
            sql = f"""
                SELECT external_id, user_id
                FROM user_external_ids
                WHERE auth_provider = ? AND external_id IN ({ids})
            """
            params = [self.config.auth_provider] + external_user_ids
            txn.execute(sql, params)
            return txn.fetchall()

        rows = await self.api._store.db_pool.runInteraction(
            "batch_get_user_by_external_id",
            _batch_get_users_txn,
        )
        if not rows:
            raise Exception(
                "No Matrix user IDs found for the provided external user IDs."
            )

        external_to_matrix = {row[0]: row[1] for row in rows}
        result = []
        not_found_ids = []
        for external_id in external_user_ids:
            if external_id in external_to_matrix:
                result.append(external_to_matrix[external_id])
            else:
                not_found_ids.append(external_id)
        if not_found_ids:
            logger.warning(
                "The following external user IDs were not found in the database: %s",
                not_found_ids,
            )

        # TODO: assert that UserID string to start with '@'
        return result
