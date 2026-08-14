import logging
from collections.abc import Collection

from pydantic import BaseModel, Field
from pydantic.dataclasses import dataclass

logger = logging.getLogger(__name__)

MANAGED_ROOM_TYPE = "de.famedly.managedRoom"
SYNC_TOKEN_TYPE = "de.famedly.roomControl.lastSyncToken.v1"
MANAGED_ROOM_RETRY_QUEUE_SNAPSHOT_TYPE_V1 = "de.famedly.managedRoomRetryQueue.v1"


@dataclass(slots=True)
class ActionReason:
    reason: str
    is_removal: bool
    retry_count: int
    first_attempt_utc_ms: int
    latest_attempt_utc_ms: int


class RoomQueueEntry(BaseModel):
    external_ids: dict[str, ActionReason] = Field(default_factory=dict)
    members: dict[str, ActionReason] = Field(default_factory=dict)

    def __bool__(self) -> bool:
        """Allow of easy check if there are ANY queue entries"""
        return bool(self.external_ids or self.members)

    def maybe_noop_or_update_external_id(
        self, id_to_compare: str, reason: ActionReason
    ) -> bool:
        """
        External user IDs being added or removed should cancel out opposing entries if
        they exist. For example, if an existing entry to remove an external id exists,
        but a new entry comes in that adds that user id, then it is a no-op and they
        should both be deleted.

        Alternatively, assigning a group to a room and updating
        groups through the sync loop can conflict on timestamps of last attempt, update
        those

        Returns: bool on if the action was a no-op/update and no further action is needed
        """
        if id_to_compare in self.external_ids:
            if self.external_ids[id_to_compare].is_removal != reason.is_removal:
                self.external_ids.pop(id_to_compare)
                return True
            self.external_ids[id_to_compare].latest_attempt_utc_ms = (
                reason.latest_attempt_utc_ms
            )
            self.external_ids[id_to_compare].retry_count += 1
            return True
        return False


class ManagedRoomRetryQueue(BaseModel):
    rooms: dict[str, RoomQueueEntry] = Field(default_factory=dict)

    def add_external_id_to_room_queue(
        self, room_id: str, external_id: str, reason: ActionReason
    ):
        entry = self.rooms.setdefault(room_id, RoomQueueEntry())
        if not entry.maybe_noop_or_update_external_id(external_id, reason):
            # This was neither a no-op removal, nor an update to the timestamp. This
            # means it did not previously exist in any way that matters. Assign it.
            logger.debug(
                "%s was not found while assigning group to room('%s'), adding to "
                "retry queue. Reason=%r",
                external_id,
                room_id,
                reason,
            )
            entry.external_ids[external_id] = reason
        # Clean up old empty room entries
        if not self.rooms[room_id]:
            self.rooms.pop(room_id)

    def add_mxid_to_room_queue(self, room_id: str, mxid: str, reason: ActionReason):
        self.rooms.setdefault(room_id, RoomQueueEntry()).members[mxid] = reason

    def maybe_remove_newly_found_entries(
        self, room_id: str, collection_of_external_ids: Collection[str]
    ) -> None:
        """
        During the sync loop process, it may be that a previously errored external user
        ID is suddenly found. Remove those from the queue, as the sync loop will handle
        the join/leave process now.
        """
        if room_id in self.rooms:
            for external_id in collection_of_external_ids:
                self.rooms[room_id].external_ids.pop(external_id, None)
            if not self.rooms[room_id]:
                self.rooms.pop(room_id)
