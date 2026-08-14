import json

from synapse.module_api import ModuleApi
from synapse.storage.engines import PostgresEngine
from synapse.types import JsonDict

from famedly_control_synapse.types import MANAGED_ROOM_TYPE, SYNC_TOKEN_TYPE

# Maps order_by field names to SQL column expressions.
# Those values are inserted directly into SQL; never use user input here.
_ORDER_BY_COLUMN: dict[str, str] = {
    "name": "rs.name",
    "canonical_alias": "rs.canonical_alias",
    "joined_members": "rc.joined_members",
    "joined_local_members": "rc.joined_local_members",
    "guest_access": "rs.guest_access",
    "history_visibility": "rs.history_visibility",
    "join_rules": "rs.join_rules",
    "encryption": "rs.encryption",
    "federatable": "rs.is_federatable",
    "public": "r.is_public",
    "state_events": "rc.current_state_events",
    "version": "r.room_version",
    "creator": "r.creator",
}

VALID_ORDER_BY_FIELDS: frozenset[str] = frozenset(_ORDER_BY_COLUMN)


class ManagedRoomRepository:
    def __init__(self, api: ModuleApi) -> None:
        self._api = api
        self._store = api._store
        self._account_data_handler = api._account_data_handler
        self._using_postgres = isinstance(self._store.db_pool.engine, PostgresEngine)

    def _build_filter_sql(
        self,
        search_term: str | None,
        managed_room_group_id: str | None,
    ) -> tuple[str, str, list]:
        """Build conditional SQL fragments and their bound parameters.

        Returns (group_filter_sql, search_filter_sql, params).
        params contains values for ? placeholders in group_filter_sql followed by
        search_filter_sql, in that order.
        """
        params: list = []

        group_filter_sql = ""
        if managed_room_group_id is not None:
            if self._using_postgres:
                group_filter_sql = """
                    AND EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements_text(ad.content::jsonb -> 'groups') gf(value)
                        WHERE gf.value = ?
                    )"""
            else:
                group_filter_sql = """
                    AND EXISTS (
                        SELECT 1 FROM json_each(json_extract(ad.content, '$.groups'))
                        WHERE value = ?
                    )"""
            params.append(managed_room_group_id)

        search_filter_sql = ""
        if search_term is not None:
            escaped = (
                search_term.lower()
                .replace("!", "!!")
                .replace("%", "!%")
                .replace("_", "!_")
            )
            pattern = f"%{escaped}%"
            alias_body = escaped.removeprefix("#")
            alias_pattern = f"#%{alias_body}%"
            search_filter_sql = """
                WHERE (
                    inner_q.room_id = ?
                    OR LOWER(rs.name) LIKE ? ESCAPE '!'
                    OR LOWER(rs.canonical_alias) LIKE ? ESCAPE '!'
                )"""
            params.extend([search_term, pattern, alias_pattern])

        return group_filter_sql, search_filter_sql, params

    async def count_managed_rooms(
        self,
        search_term: str | None = None,
        managed_room_group_id: str | None = None,
    ) -> int:
        group_filter_sql, search_filter_sql, filter_params = self._build_filter_sql(
            search_term, managed_room_group_id
        )
        rs_join = (
            "LEFT JOIN room_stats_state rs ON rs.room_id = inner_q.room_id"
            if search_filter_sql
            else ""
        )
        sql = f"""
            SELECT COUNT(*)
            FROM (
                SELECT ad.room_id
                FROM room_account_data ad
                WHERE ad.account_data_type = ?
                {group_filter_sql}
                GROUP BY ad.room_id
            ) inner_q
            {rs_join}
            {search_filter_sql}
        """
        rows = await self._store.db_pool.execute(
            "count_managed_rooms",
            sql,
            MANAGED_ROOM_TYPE,
            *filter_params,
        )
        return rows[0][0] if rows else 0

    async def get_managed_rooms_paginated(
        self,
        limit: int,
        offset: int,
        search_term: str | None = None,
        order_by: str = "name",
        direction: str = "ASC",
        managed_room_group_id: str | None = None,
    ) -> list[JsonDict]:
        group_filter_sql, search_filter_sql, filter_params = self._build_filter_sql(
            search_term, managed_room_group_id
        )
        extra_joins = (
            "LEFT JOIN rooms r ON r.room_id = inner_q.room_id"
            if order_by in ("version", "public", "creator")
            else ""
        )

        order_col = _ORDER_BY_COLUMN[order_by]
        if self._using_postgres:
            agg_select = "JSONB_AGG(DISTINCT g.value) FILTER (WHERE g.value IS NOT NULL) AS merged_groups"
            array_join = "LEFT JOIN jsonb_array_elements_text(ad.content::jsonb -> 'groups') g(value) ON TRUE"
            order_by_sql = (
                f"ORDER BY {order_col} {direction} NULLS LAST, inner_q.room_id ASC"
            )
        else:
            agg_select = "JSON_GROUP_ARRAY(DISTINCT g.value) FILTER (WHERE g.value IS NOT NULL) AS merged_groups"
            array_join = (
                "LEFT JOIN json_each(json_extract(ad.content, '$.groups')) g ON TRUE"
            )
            # Use CASE to put NULLs last regardless of direction
            order_by_sql = f"ORDER BY CASE WHEN {order_col} IS NULL THEN 1 ELSE 0 END, {order_col} {direction}, inner_q.room_id ASC"

        sql = f"""
            SELECT
                inner_q.room_id,
                inner_q.merged_groups,
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
                    {agg_select}
                FROM room_account_data ad
                {array_join}
                WHERE ad.account_data_type = ?
                {group_filter_sql}
                GROUP BY ad.room_id
            ) inner_q
            LEFT JOIN room_stats_state rs ON rs.room_id = inner_q.room_id
            LEFT JOIN room_stats_current rc ON rc.room_id = inner_q.room_id
            {extra_joins}
            {search_filter_sql}
            {order_by_sql}
            LIMIT ? OFFSET ?
        """
        rows = await self._store.db_pool.execute(
            "get_managed_rooms_paginated",
            sql,
            MANAGED_ROOM_TYPE,
            *filter_params,
            limit,
            offset,
        )

        return [self._row_to_room_entry(row) for row in rows]

    async def get_managed_room(self, room_id: str) -> JsonDict | None:
        """Fetch a single managed room as a ManagedRoomChunk.

        Aggregates the assigned groups from the room's ``de.famedly.managedRoom``
        account data and joins the room stats, mirroring the shape of the entries
        returned by `get_managed_rooms_paginated()`.

        Returns:
            A ManagedRoomChunk dict, or None if the room is not a managed room.
        """
        if self._using_postgres:
            agg_select = "JSONB_AGG(DISTINCT g.value) FILTER (WHERE g.value IS NOT NULL) AS merged_groups"
            array_join = "LEFT JOIN jsonb_array_elements_text(ad.content::jsonb -> 'groups') g(value) ON TRUE"
        else:
            agg_select = "JSON_GROUP_ARRAY(DISTINCT g.value) FILTER (WHERE g.value IS NOT NULL) AS merged_groups"
            array_join = (
                "LEFT JOIN json_each(json_extract(ad.content, '$.groups')) g ON TRUE"
            )

        sql = f"""
            SELECT
                inner_q.room_id,
                inner_q.merged_groups,
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
                    {agg_select}
                FROM room_account_data ad
                {array_join}
                WHERE ad.account_data_type = ?
                    AND ad.room_id = ?
                GROUP BY ad.room_id
            ) inner_q
            LEFT JOIN room_stats_state rs ON rs.room_id = inner_q.room_id
            LEFT JOIN room_stats_current rc ON rc.room_id = inner_q.room_id
        """
        rows = await self._store.db_pool.execute(
            "get_managed_room",
            sql,
            MANAGED_ROOM_TYPE,
            room_id,
        )
        if not rows:
            return None
        return self._row_to_room_entry(rows[0])

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
