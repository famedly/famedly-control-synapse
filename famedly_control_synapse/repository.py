import json

from synapse.module_api import ModuleApi
from synapse.storage.engines import PostgresEngine
from synapse.types import JsonDict

from famedly_control_synapse.rest.types import MANAGED_ROOM_TYPE, SYNC_TOKEN_TYPE


class ManagedRoomRepository:
    def __init__(self, api: ModuleApi) -> None:
        self._api = api
        self._store = api._store
        self._account_data_handler = api._account_data_handler
        self._using_postgres = isinstance(self._store.db_pool.engine, PostgresEngine)

    async def count_managed_rooms(self) -> int:
        rows = await self._store.db_pool.execute(
            "count_managed_rooms",
            "SELECT COUNT(DISTINCT room_id) FROM room_account_data WHERE account_data_type = ?",
            MANAGED_ROOM_TYPE,
        )
        return rows[0][0] if rows else 0

    async def get_managed_rooms_paginated(
        self, limit: int, offset: int
    ) -> list[JsonDict]:
        if self._using_postgres:
            sql = """
                SELECT
                    paged.room_id,
                    paged.merged_groups,
                    rs.name,
                    rs.topic,
                    rs.canonical_alias,
                    rs.avatar,
                    rs.guest_access,
                    rs.history_visibility,
                    rc.joined_members
                FROM (
                    SELECT
                        ad.room_id,
                        JSONB_AGG(DISTINCT g.value) AS merged_groups

                    FROM room_account_data ad
                    LEFT JOIN jsonb_array_elements_text(ad.content::jsonb -> 'groups') g(value) ON TRUE
                    WHERE ad.account_data_type = ?
                    GROUP BY ad.room_id
                    ORDER BY ad.room_id
                    LIMIT ? OFFSET ?
                ) paged
                LEFT JOIN room_stats_state rs ON rs.room_id = paged.room_id
                LEFT JOIN room_stats_current rc ON rc.room_id = paged.room_id
            """
        else:
            sql = """
                SELECT
                    paged.room_id,
                    paged.merged_groups,
                    rs.name,
                    rs.topic,
                    rs.canonical_alias,
                    rs.avatar,
                    rs.guest_access,
                    rs.history_visibility,
                    rc.joined_members
                FROM (
                    SELECT
                        ad.room_id,
                        JSON_GROUP_ARRAY(DISTINCT g.value) AS merged_groups
                    FROM room_account_data ad
                    LEFT JOIN json_each(json_extract(ad.content, '$.groups')) g ON TRUE
                    WHERE ad.account_data_type = ?
                    GROUP BY ad.room_id
                    ORDER BY ad.room_id
                    LIMIT ? OFFSET ?
                ) paged
                LEFT JOIN room_stats_state rs ON rs.room_id = paged.room_id
                LEFT JOIN room_stats_current rc ON rc.room_id = paged.room_id
            """
        rows = await self._store.db_pool.execute(
            "get_managed_rooms_paginated",
            sql,
            MANAGED_ROOM_TYPE,
            limit,
            offset,
        )

        return [self._row_to_room_entry(row) for row in rows]

    async def get_rooms_by_group(self) -> dict[str, list[tuple[str, str]]]:
        """Get a mapping of group_id to list of (room_id, admin_user_id).

        The admin is the sender of the m.room.create event.
        Only rooms where the account_data user_id matches the room creator are returned.

        Returns:
            Dict mapping group IDs to lists of (room_id, admin_user_id) tuples.
        """
        rows = await self._store.db_pool.execute(
            "get_all_managed_rooms_with_groups",
            """SELECT ad.room_id, e.sender, ad.content
            FROM room_account_data ad
            INNER JOIN current_state_events cse
                ON cse.room_id = ad.room_id
                AND cse.type = 'm.room.create'
                AND cse.state_key = ''
            INNER JOIN events e
                ON e.event_id = cse.event_id
            WHERE ad.account_data_type = ?
                AND ad.user_id = e.sender""",
            MANAGED_ROOM_TYPE,
        )

        result: dict[str, list[tuple[str, str]]] = {}
        for room_id, sender, content_raw in rows:
            content = (
                json.loads(content_raw) if isinstance(content_raw, str) else content_raw
            )
            groups = content.get("groups", [])
            for group_id in groups:
                result.setdefault(group_id, []).append((room_id, sender))

        return result

    async def get_sync_token_entry(self) -> tuple[str, str | None] | None:
        """Load the persisted sync token and its owning user.

        If multiple entries exist, take any owned by a server admin.

        Returns:
            A (user_id, token) tuple, or None if no entry exists.
            The token itself may be None if this is the initial state.
        """
        rows = await self._store.db_pool.execute(
            "get_sync_token_entry",
            "SELECT user_id, content FROM account_data WHERE account_data_type = ?",
            SYNC_TOKEN_TYPE,
        )
        if not rows:
            return None

        for user_id, content_raw in rows:
            if await self._api.is_user_admin(user_id):
                content = (
                    json.loads(content_raw)
                    if isinstance(content_raw, str)
                    else content_raw
                )
                return user_id, content.get("token")

        # no admin found associated with SYNC_TOKEN_TYPE
        return None

    async def set_sync_token(self, user_id: str, token: str | None) -> None:
        """Persist the sync token as global account data for the given user."""
        await self._account_data_handler.add_account_data_for_user(
            user_id, SYNC_TOKEN_TYPE, {"token": token}
        )

    async def initialize_sync_token(self, user_id: str) -> None:
        """Create the sync token account data if it doesn't exist yet."""
        existing = await self.get_sync_token_entry()
        if existing is None:
            await self.set_sync_token(user_id, None)

    # TODO: add caching for this since it's called for every membership event
    async def is_managed_room(self, room_id: str, admin_user_id: str) -> bool:
        rows = await self._store.db_pool.execute(
            "is_managed_room",
            """SELECT 1 FROM room_account_data WHERE
            user_id = ? AND
            room_id = ? AND
            account_data_type = ?
            LIMIT 1""",
            admin_user_id,
            room_id,
            MANAGED_ROOM_TYPE,
        )
        return bool(rows)

    @staticmethod
    def _row_to_room_entry(row: tuple) -> JsonDict:
        (
            room_id,
            groups_raw,
            name,
            topic,
            canonical_alias,
            avatar,
            guest_access,
            history_visibility,
            joined_members,
        ) = row

        if groups_raw:
            groups = (
                json.loads(groups_raw) if isinstance(groups_raw, str) else groups_raw
            )
            groups = sorted(groups)
        else:
            groups = []

        entry: JsonDict = {
            "room_id": room_id,
            MANAGED_ROOM_TYPE: {"groups": groups},
        }
        if name:
            entry["name"] = name
        if topic:
            entry["topic"] = topic
        if canonical_alias:
            entry["canonical_alias"] = canonical_alias
        if avatar:
            entry["avatar_url"] = avatar
        if history_visibility is not None:
            entry["world_readable"] = history_visibility == "world_readable"
        if guest_access is not None:
            entry["guest_can_join"] = guest_access == "can_join"
        if joined_members is not None:
            entry["num_joined_members"] = joined_members

        return entry
