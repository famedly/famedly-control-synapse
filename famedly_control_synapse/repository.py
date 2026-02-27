import json

from synapse.module_api import ModuleApi
from synapse.storage.engines import PostgresEngine
from synapse.types import JsonDict

from famedly_control_synapse.types import MANAGED_ROOM_TYPE


class ManagedRoomRepository:
    def __init__(self, api: ModuleApi) -> None:
        self._store = api._store
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

    async def is_managed_room(self, room_id: str) -> bool:
        rows = await self._store.db_pool.execute(
            "is_managed_room",
            "SELECT 1 FROM room_account_data WHERE room_id = ? AND account_data_type = ? LIMIT 1",
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
