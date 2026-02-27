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
        self, room_id: str, users: list[str], requester: Requester
    ) -> None:
        """Force join users to a managed room that is invite-only.

        Args:
            room_id: The ID of the room to join.
            users: The list of external user IDs to join.
            requester: The requester who is the admin/room creator performing the action.
        """
        users = await self.batch_convert_external_user_ids_to_matrix_user_ids(users)
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
                logger.exception(
                    "Failed to update room membership for %s: %s", member, e
                )

    async def batch_convert_external_user_ids_to_matrix_user_ids(
        self, external_ids: list[str]
    ) -> list[str]:
        """Convert multiple external user IDs to Matrix user IDs in a single database query.

        Args:
            external_ids: List of external IDs to convert.

        Returns:
            List of Matrix user IDs corresponding to the provided external IDs.
        """
        if not external_ids:
            return []

        def _batch_get_users_txn(txn):
            ids = ",".join("?" * len(external_ids))
            sql = f"""
                SELECT external_id, user_id
                FROM user_external_ids
                WHERE auth_provider = ? AND external_id IN ({ids})
            """
            params = [self.config.auth_provider] + external_ids
            txn.execute(sql, params)
            return txn.fetchall()

        rows = await self.api._store.db_pool.runInteraction(
            "batch_get_user_by_external_id",
            _batch_get_users_txn,
        )
        if not rows:
            raise Exception("No Matrix user IDs found for the provided external IDs.")

        external_to_matrix = {row[0]: row[1] for row in rows}
        result = []
        not_found_ids = []
        for external_id in external_ids:
            if external_id in external_to_matrix:
                result.append(external_to_matrix[external_id])
            else:
                not_found_ids.append(external_id)
        if not_found_ids:
            logger.warning(
                "The following external IDs were not found in the database: %s",
                not_found_ids,
            )
        return result
