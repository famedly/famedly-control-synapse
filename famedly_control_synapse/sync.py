# Copyright (C) 2026 Famedly
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
import logging

from synapse.module_api import ModuleApi
from synapse.util.duration import Duration

from famedly_control_synapse.client import (
    DiffRecord,
    FamedlyControlClient,
    MembershipAction,
)
from famedly_control_synapse.config import FamedlyControlConfig
from famedly_control_synapse.repository import ManagedRoomRepository
from famedly_control_synapse.room_handler import ManagedRoomHandler

logger = logging.getLogger(__name__)


class GroupMembershipSyncer:
    """Background task that polls Famedly Control for group membership changes
    and updates managed room memberships accordingly."""

    def __init__(
        self,
        api: ModuleApi,
        client: FamedlyControlClient,
        room_handler: ManagedRoomHandler,
        repository: ManagedRoomRepository,
        config: FamedlyControlConfig,
    ) -> None:
        self.api = api
        self.client = client
        self.room_handler = room_handler
        self.repository = repository
        self.server_name = api.server_name
        self.polling_interval_seconds = config.sync_polling_interval_seconds
        self.is_enabled = config.sync_enabled
        self._sync_token: str | None = None
        self._sync_token_user_id: str | None = None
        self._is_running = False

    def start(self) -> None:
        """
        Start the periodic background sync.

        Safe to call multiple times, does nothing if already running.
        Schedules `start_sync_loop()` as a background process which
        checks for a sync token; if one exists it sets up a loop that
        periodically calls `_process_sync()`
        """
        if self._is_running or not self.is_enabled:
            return
        # Mark the loop as starting early, to avoid multiple background loops
        self._is_running = True
        self.api.run_as_background_process(
            "famedly_control_group_membership_sync", self.start_sync_loop
        )

    async def start_sync_loop(self) -> None:
        """Initialize state and start the periodic sync.

        If the sync token hasn't been loaded yet, tries to read it from the
        database.  If it's still not available (no managed room created yet),
        returns and will be retried on the next api request. Otherwise, sets
        up the periodic ``_process_sync``.
        """
        entry = await self.repository.get_sync_token_entry()
        if entry is None:
            self._is_running = False
            return

        logger.info("Starting loop to poll /get_all_groups_diffs")

        self._sync_token_user_id, self._sync_token = entry

        while self._is_running:
            try:
                await self._process_sync()
            # Several kinds of exceptions can be raised here. Timeouts, Cancellations,
            # Network Exceptions, HTTP Exceptions, general exceptions from database or
            # IO, etc. I believe the CancelledError from twisted.defer can be raised
            # from the sleep() below. Perhaps in the future that should be caught, it
            # should only occur during server shutdown though and can safely ignored
            # (although it may look rather scary in the logs)
            except Exception as e:
                logger.error("Exception during loop: %r", e)
                await self.api._hs.get_clock().sleep(
                    Duration(seconds=self.polling_interval_seconds)
                )

    async def _process_sync(self) -> None:
        """
        Execute a single sync iteration: fetch diffs and apply membership changes.

        Long poll for the response. If a 'next_sync' token is returned, use that on the
        next request.
        """
        response = await self.client.get_all_groups_diffs(
            sync=self._sync_token, timeout=self.polling_interval_seconds
        )

        if response.next_sync == self._sync_token:
            return

        sync_succeeded = True

        if response.data:
            group_rooms = await self.repository.get_rooms_by_group()

            for group_id, diffs in response.data.items():
                rooms = group_rooms.get(group_id, [])
                if not rooms:
                    logger.debug(
                        "Received diff for group %s but no managed rooms use it",
                        group_id,
                    )
                    continue

                for room_id, admin_user_id in rooms:
                    if not await self._apply_diffs(room_id, admin_user_id, diffs):
                        sync_succeeded = False

        if not sync_succeeded:
            logger.warning(
                "Some membership updates failed during sync, will retry next cycle",
            )
            return

        self._sync_token = response.next_sync
        if self._sync_token_user_id is None:
            logger.error("Loop was running but there was no user id for the sync token")
        else:
            await self.repository.set_sync_token(
                self._sync_token_user_id, self._sync_token
            )

    async def _apply_diffs(
        self, room_id: str, admin_user_id: str, diffs: list[DiffRecord]
    ) -> bool:
        """Apply membership diffs to a single room.

        Returns:
            True if all operations succeeded, False if any failed.
        """
        external_ids_to_add = [
            d.external_user_id for d in diffs if d.action == MembershipAction.ADD
        ]
        external_ids_to_remove = [
            d.external_user_id for d in diffs if d.action == MembershipAction.REM
        ]

        result = await self.room_handler.apply_membership_changes_from_external_ids(
            room_id=room_id,
            admin_user_id=admin_user_id,
            external_ids_to_add=external_ids_to_add,
            external_ids_to_remove=external_ids_to_remove,
        )

        if result.has_errors:
            if result.not_found_ids:
                logger.warning(
                    "Could not resolve external IDs for room %s: %s",
                    room_id,
                    result.not_found_ids,
                )
            if result.join_errors:
                logger.warning(
                    "Failed to add some users to room %s: %s",
                    room_id,
                    result.join_errors,
                )
            if result.leave_errors:
                logger.warning(
                    "Failed to remove some users from room %s: %s",
                    room_id,
                    result.leave_errors,
                )
            return False

        return True
