import logging
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from http import HTTPStatus
from itertools import chain

from prometheus_client import Counter, Gauge
from synapse.api.constants import EventContentFields, EventTypes, Membership
from synapse.api.errors import NotFoundError, UnstableSpecAuthError
from synapse.events import EventBase
from synapse.logging.context import make_deferred_yieldable
from synapse.metrics import SERVER_NAME_LABEL
from synapse.module_api import ModuleApi
from synapse.module_api.errors import Codes
from synapse.types import StateMap, UserID, create_requester
from synapse.util.duration import Duration
from twisted.internet.defer import Deferred, DeferredLock

from famedly_control_synapse.client import FamedlyControlClient, FamedlyControlError
from famedly_control_synapse.config import FamedlyControlConfig
from famedly_control_synapse.repository import ManagedRoomRepository
from famedly_control_synapse.types import (
    MANAGED_ROOM_RETRY_QUEUE_SNAPSHOT_TYPE_V1,
    MANAGED_ROOM_TYPE,
    ActionReason,
    ManagedRoomRetryQueue,
)

logger = logging.getLogger(__name__)

CREATE_EVENT_FILTER = (EventTypes.Create, "")

famedly_control_user_sync_error = Counter(
    "famedly_control_user_sync_error",
    "Counts failures when updating user membership in managed rooms. `error_code` is "
    "the Synapse errcode derived from the exception, and `server_name` is the local "
    "homeserver where the error occurred.",
    labelnames=["error_code", SERVER_NAME_LABEL],
)


# Known queue properties for the below metric:
#   "number_of_rooms"
#   "distinct_external_user_ids"
#   "distinct_mxids"
#   "joins"
#   "leaves"
famedly_control_error_retry_queue_properties = Gauge(
    "famedly_control_error_retry_queue_properties",
    "Monitor various queue properties such as size of the queue and counts of distinct "
    "elements inside. `queue_property` is the name of the property, and `server_name` is the "
    "server's name where the error occurred.",
    labelnames=[
        "queue_property",
        SERVER_NAME_LABEL,
    ],
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
    def __init__(
        self,
        api: ModuleApi,
        config: FamedlyControlConfig,
        client: FamedlyControlClient,
        repository: ManagedRoomRepository,
    ):
        self.api = api
        self.config = config
        self.client = client
        self.account_data_handler = self.api._hs.get_account_data_handler()
        self.server_name = api.server_name
        self.admin_user = UserID(config.admin_user, self.server_name).to_string()
        self.repository = repository
        self.retry_queue = ManagedRoomRetryQueue()
        self.retry_queue_process_running = False
        self.retry_queue_lock = DeferredLock()
        self.clock = api._clock

    def _load_queue_snapshot(self) -> None:
        """
        One-shot attempt at loading either a fresh queue or a saved snapshot and
        beginning the retry loop
        """
        d = Deferred.fromCoroutine(self._after_startup())
        # Whenever a raw Deferred is created and ran, it needs to be wrapped into a log
        # context. 'make_deferred_yieldable()' does that for us
        make_deferred_yieldable(d)

    async def _after_startup(self) -> None:
        """
        To be called when the reactor is running. Loads the snapshot of the retry queue
        and begins the background loop to retry entries from the queue.
        """
        # This whole process only works if a managed room has been created. That
        # triggers saving a copy of the room creator for the sync loop to function. If
        # there was no room, then there can be no queue, and therefore a blank one can
        # be made. It will not be persisted until the first room is created.
        admin_user_id = await self.get_admin_user_id_from_sync_token()

        self.retry_queue = await self.get_retry_queue_snapshot(admin_user_id)
        if not self.config.error_retry_queue_enabled:
            return

        self.clock.looping_call(
            self.process_retry_queue,
            Duration(seconds=self.config.error_retry_queue_interval_seconds),
            admin_user_id,
        )

    async def get_admin_user_id_from_sync_token(self) -> str | None:
        """
        Try and acquire the admin user/room creator from the sync token that exists
        after the first managed room is created
        """
        sync_token_entry = await self.repository.get_sync_token_entry()
        if sync_token_entry is None:
            # If no sync token entry exists yet, load a blank retry queue. This is most
            # likely to occur during startup before there are any managed rooms.
            admin_user_id = None
        else:
            admin_user_id, _ = sync_token_entry
        return admin_user_id

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

    async def get_users_room_membership(
        self, room_id: str, list_of_mxids: Collection[str]
    ) -> StateMap[EventBase]:
        """Helper to retrieve a state mapping for the room for only the given users"""
        state_keys = tuple((EventTypes.Member, mxid) for mxid in list_of_mxids)
        return await self.api.get_room_state(room_id, state_keys)

    async def force_join_users_to_room(
        self, room_id: str, user_mxids: Collection[str], admin_user_id: str
    ) -> dict[str, str]:
        """Force join users to a managed room that is invite-only.

        Args:
            room_id: The ID of the room to join.
            user_mxids: The list of Matrix user IDs to join.
            admin_user_id: The requester who is the admin/room creator performing the action.

        Returns: A dict containing any errors keyed by user. Can be empty
        """
        errors: dict[str, str] = {}
        if not user_mxids:
            return errors

        # Prepare a state mapping for the users that are interesting
        state_map = await self.get_users_room_membership(room_id, user_mxids)
        requester = create_requester(admin_user_id)
        for member in user_mxids:
            try:
                existing_member = await self.api.check_user_exists(member)
                if existing_member is None:
                    errors[member] = "User does not exist on this server"
                    logger.error("User %s does not exist, skipping", member)
                    self.increment_error_count(error_code=Codes.NOT_FOUND.value)
                    continue
                deactivated = await self.api._store.get_user_deactivated_status(member)
                if deactivated:
                    logger.debug("User %s is deactivated, skipping force join", member)
                    self.increment_error_count(error_code=Codes.USER_DEACTIVATED.value)
                    # Do not include this as an error to return. It is not actionable
                    continue

                fake_requester = create_requester(
                    existing_member, authenticated_entity=requester.authenticated_entity
                )
                membership_in_room = get_membership_for_user(member, state_map)

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
            except Exception as e:  # noqa: BLE001 Do not catch blind exception
                # Catch any other unexpected exceptions
                error_msg = str(e)
                errors[member] = error_msg
                logger.error("Failed to update room membership for %s: %s", member, e)
                self.increment_error_count(
                    error_code=self.get_error_code_from_exception(e)
                )
        return errors

    async def _assert_room_exists(self, room_id: str) -> None:
        """
        Does the room still exist? Raises a `NotFoundError` if it does not.
        """
        # A simple check that a room exists using a cached database call to verify that, so should be fairly low
        # impact. Discard the value as it has no use, if the room doesn't exist it will raise the NotFoundError.
        await self.api._hs.get_datastores().main.get_room_version_id(room_id)

    async def maybe_drop_room_from_retry_queue(self, room_id: str) -> bool:
        """
        Drop the room from the retry queue so it will not be tried again if it does not exist.

        Args:
            room_id: The ID of the room to drop.
            lock_held: bool to indicate that the lock is already held by an external calling function.

        Returns:
            Bool describing if the room does not exist.
        """
        try:
            # Raises `NotFoundError` if the room is unknown/doesn't exist.
            await self._assert_room_exists(room_id)
            return False
        except NotFoundError:
            # The room does not exist any more, just remove the entries and drop them on the floor. Apply the default
            # to avoid a KeyError exception, just in case it was already dropped by some other process trying to
            # compete.
            async with self.retry_queue_lock:
                self.retry_queue.rooms.pop(room_id, None)
                await self.save_retry_queue_snapshot(self.admin_user)
            # TODO: add in metrics for this? api.errors.Codes does not have a proper "M_UNKNOWN_ROOM" code to use
            return True

    async def remove_users_from_room(
        self, creator_id: str, user_mxids: Collection[str], room_id: str
    ) -> dict[str, str]:
        """Force remove users from a managed room.

        Args:
            creator_id: The ID of the room's creator, for creating the leave event
            room_id: The ID of the room.
            user_mxids: The list of Matrix user IDs to kick.

        Returns: A dict containing any errors keyed by user. Can be empty
        """
        errors: dict[str, str] = {}
        if not user_mxids:
            return errors
        # Prepare a state mapping for the users that are interesting
        state_map = await self.get_users_room_membership(room_id, user_mxids)
        for member in user_mxids:
            try:
                existing_member = await self.api.check_user_exists(member)
                if existing_member is None:
                    errors[member] = "User does not exist on this server"
                    logger.error("User %s does not exist, skipping removal", member)
                    self.increment_error_count(error_code=Codes.NOT_FOUND.value)
                    continue
                if get_membership_for_user(member, state_map) == Membership.LEAVE:
                    # This user is not in the room, skip it
                    continue

                await self.api.update_room_membership(
                    sender=creator_id,
                    target=existing_member,
                    room_id=room_id,
                    new_membership=Membership.LEAVE,
                    content={"reason": "User has been removed from the room"},
                )
            except Exception as e:  # noqa: BLE001 Do not catch blind exception
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
        mxids_to_add: Collection[str],
        mxids_to_remove: Collection[str],
    ) -> None:
        """Apply membership changes to a room using Matrix user IDs.

        Args:
            room_id: The room to modify.
            admin_user_id: The admin user performing the changes.
            mxids_to_add: Matrix user IDs to invite/join. Can be empty list
            mxids_to_remove: Matrix user IDs to remove. Can be empty list
        """
        join_errors = await self.force_join_users_to_room(
            room_id, mxids_to_add, admin_user_id
        )
        leave_errors = await self.remove_users_from_room(
            admin_user_id, mxids_to_remove, room_id
        )
        async with self.retry_queue_lock:
            for mxid, error_reason in join_errors.items():
                self.retry_queue.add_mxid_to_room_queue(
                    room_id,
                    mxid,
                    ActionReason(
                        reason=error_reason,
                        is_removal=False,
                        retry_count=0,
                        first_attempt_utc_ms=self.clock.time_msec(),
                        latest_attempt_utc_ms=self.clock.time_msec(),
                    ),
                )
            for mxid, error_reason in leave_errors.items():
                self.retry_queue.add_mxid_to_room_queue(
                    room_id,
                    mxid,
                    ActionReason(
                        reason=error_reason,
                        is_removal=True,
                        retry_count=0,
                        first_attempt_utc_ms=self.clock.time_msec(),
                        latest_attempt_utc_ms=self.clock.time_msec(),
                    ),
                )

            # Save the queue, all updates to it should take place by now
            await self.save_retry_queue_snapshot(admin_user_id)

    async def apply_membership_changes_from_external_ids(
        self,
        room_id: str,
        admin_user_id: str,
        external_ids_to_add: list[str],
        external_ids_to_remove: list[str],
    ) -> None:
        """Convert external user IDs and apply membership changes to a room.

        Args:
            room_id: The room to modify.
            admin_user_id: The admin user performing the changes.
            external_ids_to_add: External user IDs to invite/join. Can be empty list
            external_ids_to_remove: External user IDs to remove. Can be empty list
        """
        # First find out if the room even still exists
        if await self.maybe_drop_room_from_retry_queue(room_id):
            # There seems to be nothing more to do here, just return
            return

        mxids_to_add_mapping = (
            await self.batch_convert_external_user_ids_to_matrix_user_ids(
                external_ids_to_add
            )
        )
        adds_not_found = parse_missing_items(
            mxids_to_add_mapping.keys(), external_ids_to_add
        )

        mxids_to_remove_mapping = (
            await self.batch_convert_external_user_ids_to_matrix_user_ids(
                external_ids_to_remove
            )
        )
        removes_not_found = parse_missing_items(
            mxids_to_remove_mapping.keys(), external_ids_to_remove
        )

        async with self.retry_queue_lock:
            for external_id in adds_not_found:
                self.retry_queue.add_external_id_to_room_queue(
                    room_id,
                    external_id,
                    ActionReason(
                        reason="External User ID mapping not Found",
                        is_removal=False,
                        retry_count=0,
                        first_attempt_utc_ms=self.clock.time_msec(),
                        latest_attempt_utc_ms=self.clock.time_msec(),
                    ),
                )

            self.retry_queue.maybe_remove_newly_found_entries(
                room_id, mxids_to_add_mapping.keys()
            )
            for external_id in removes_not_found:
                self.retry_queue.add_external_id_to_room_queue(
                    room_id,
                    external_id,
                    ActionReason(
                        reason="External User ID mapping not Found",
                        is_removal=True,
                        retry_count=0,
                        first_attempt_utc_ms=self.clock.time_msec(),
                        latest_attempt_utc_ms=self.clock.time_msec(),
                    ),
                )

            self.retry_queue.maybe_remove_newly_found_entries(
                room_id, mxids_to_remove_mapping.keys()
            )

        await self.apply_membership_changes(
            room_id,
            admin_user_id,
            mxids_to_add_mapping.values(),
            mxids_to_remove_mapping.values(),
        )

    async def batch_convert_external_user_ids_to_matrix_user_ids(
        self, external_user_ids: Iterable[str]
    ) -> dict[str, str]:
        """Convert multiple external user IDs to Matrix user IDs in a single database query.

        Args:
            external_user_ids: List of external user IDs to convert.

        Returns:
            Mapping of external user ID -> Matrix user IDs
        """
        if not external_user_ids:
            return {}

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
        not_found_ids = [
            external_id
            for external_id in external_user_ids
            if external_id not in external_to_matrix
        ]
        if not_found_ids:
            logger.warning(
                "The following external user IDs from '%s' were not found: %s",
                self.config.auth_provider,
                not_found_ids,
            )

        return external_to_matrix

    async def get_room_creator(self, room_id: str) -> str:
        """Get the room creator from the m.room.create event.

        Args:
            room_id: The room ID to get the creator for.

        Returns:
            The Matrix user ID of the room creator.

        Raises: NotFoundError if the room does not exist.
        """
        # This function will raise `NotFoundError`
        await self._assert_room_exists(room_id)
        state_map = await self.api.get_room_state(
            room_id=room_id, event_filter=[CREATE_EVENT_FILTER]
        )
        create_event = state_map.get(CREATE_EVENT_FILTER)
        if not create_event:
            logger.warning(
                "Failed to get room creator for %s: Creation event not found", room_id
            )
            raise FamedlyControlError(
                500, f"Create event not found for room: {room_id}"
            )
        # Room versions ≤10 have explicit creator field
        creator_from_content = create_event.content.get("creator")
        # Room versions 11+ use sender. In theory, they should both be the same and
        # only the sender is guaranteed in both spots
        sender = create_event.sender
        return creator_from_content or sender

    async def fetch_group_members(self, list_of_groups: list[str]) -> set[str]:
        """
        Fetch the union of external user IDs across all of the given groups.

        This is separated out so that it can be called before creating a managed room:
        if fetching the members fails (e.g. the Famedly Control API is unreachable), the
        room creation can be skipped entirely instead of leaving behind a partial room.

        Raises:
            FamedlyControlError: If fetching the members of any group fails.
        """
        expected_member_external_ids: set[str] = set()
        for group_id in list_of_groups:
            members = await self.client.get_group_members(group_id)
            if not members:
                logger.warning("The requested group had no members: %s", group_id)

            expected_member_external_ids.update(members)
        return expected_member_external_ids

    async def assign_groups_to_room(
        self,
        room_id: str,
        admin_user: str,
        list_of_groups: list[str],
        expected_member_external_ids: set[str] | None = None,
        external_to_mxid_mapping: dict[str, str] | None = None,
    ) -> None:
        """
        Provided a list of group IDs, parse and determine the external users that are
        related and translate those into local Synapse users. Then adjust the related
        room membership to reflect the change.

        This may be used when answering the request to assign groups to a room, or when
        creating a room. A retry queue is provided for gracefully handling members that
        do not exist yet.

        When the caller has already fetched the group members (e.g. before creating the
        room to avoid a partial room), they can be passed in via
        ``expected_member_external_ids`` to avoid fetching them again. And the same with
        ``external_to_mxid_mapping`` where the database has already been checked. NOTE:
        ``external_to_mxid_mapping`` may have more member entries than are appropriate
        for a given room; do not rely on that being the source of truth but rather use
        ``expected_member_external_ids``.
        """
        # Get the new groups member state (desired state after update)
        if expected_member_external_ids is None:
            try:
                expected_member_external_ids = await self.fetch_group_members(
                    list_of_groups
                )
            except FamedlyControlError as e:
                raise FamedlyControlError(
                    e.code,
                    e.msg,
                    additional_fields={"room_id": room_id, "groups": list_of_groups},
                )

        # One way or the other, this should not be None anymore. At worst, it will be a empty set() if the group had no
        # members.
        assert expected_member_external_ids is not None

        # Verify that a room exists before making any changes
        try:
            room_creator = await self.get_room_creator(room_id)
        except NotFoundError:
            # uh, oh. Lost a room somewhere
            logger.warning(
                "The room requested for having groups assigned does not appear to exist: %s",
                room_id,
            )
            await self.maybe_drop_room_from_retry_queue(room_id)
            # For lack of a better errcode, just say it is an unknown
            raise FamedlyControlError(
                HTTPStatus.NOT_FOUND,
                "Room is unknown or otherwise doesn't exist",
                Codes.UNKNOWN,
            )

        if external_to_mxid_mapping is None:
            external_to_mxid_mapping = (
                await self.batch_convert_external_user_ids_to_matrix_user_ids(
                    expected_member_external_ids
                )
            )
        not_found_external_ids = parse_missing_items(
            external_to_mxid_mapping.keys(), expected_member_external_ids
        )

        # Update room account data with new groups information
        await self.account_data_handler.add_account_data_to_room(
            admin_user,
            room_id,
            MANAGED_ROOM_TYPE,
            {"groups": list_of_groups},
        )

        async with self.retry_queue_lock:
            if room_id in self.retry_queue.rooms:
                # Since assigning groups to a room is a complete set of what is supposed to
                # be in the room, and not a difference from a previous assignment, reset the
                # queue. All external IDs inside will be for joining, mxids will be
                # processed later.
                self.retry_queue.rooms.pop(room_id)
            for external_id in not_found_external_ids:
                self.retry_queue.add_external_id_to_room_queue(
                    room_id,
                    external_id,
                    ActionReason(
                        reason="External User ID mapping not Found",
                        is_removal=False,
                        retry_count=0,
                        first_attempt_utc_ms=self.clock.time_msec(),
                        latest_attempt_utc_ms=self.clock.time_msec(),
                    ),
                )

        expected_members = {
            external_to_mxid_mapping[e_id]
            for e_id in expected_member_external_ids
            if e_id in external_to_mxid_mapping
        }

        # Get the current members of the room
        current_member_mxids = await self.api._store.get_users_in_room(room_id)
        current_members = set(current_member_mxids)

        # Calculate membership changes
        members_to_add = expected_members - current_members
        members_to_remove = current_members - expected_members

        if room_creator:
            members_to_add.discard(room_creator)
            members_to_remove.discard(room_creator)
        members_to_add.discard(admin_user)
        members_to_remove.discard(admin_user)

        # Apply membership changes
        await self.apply_membership_changes(
            room_id, admin_user, list(members_to_add), list(members_to_remove)
        )

    async def get_retry_queue_snapshot(
        self, admin_user_id: str | None
    ) -> ManagedRoomRetryQueue:
        """
        Load the retry queue snapshot. Should only happen relatively early during
        startup
        """
        if admin_user_id is None:
            # This can occur before a managed room has been created. Always have a basic
            # empty queue in place in that instance.
            main_queue = ManagedRoomRetryQueue()
        else:
            _main_queue = (
                await self.api._store.get_global_account_data_by_type_for_user(
                    admin_user_id, MANAGED_ROOM_RETRY_QUEUE_SNAPSHOT_TYPE_V1
                )
            )
            # It is possible but unlikely that a previous attempt to save a queue
            # failed. With that possibility, a blank retry queue should be provided or
            # everything fails loudly. This should only be a circumstance if a server
            # restart occurs between the time the first room is created and the first
            # members are added that do not exist. Normally a completely empty queue
            # would be a simple empty JSON object('{}') which does not fit these
            # criteria.
            if _main_queue is None:
                main_queue = ManagedRoomRetryQueue()
            else:
                main_queue = ManagedRoomRetryQueue.model_validate(_main_queue)

        return main_queue

    async def save_retry_queue_snapshot(self, admin_user_id: str):
        """
        Save the queue as a snapshot so existing entries will not be lost after a
        restart of the server
        """
        await self.account_data_handler.add_account_data_for_user(
            admin_user_id,
            MANAGED_ROOM_RETRY_QUEUE_SNAPSHOT_TYPE_V1,
            self.retry_queue.model_dump(),
        )

    async def process_retry_queue(self, admin_user_id: str | None = None) -> None:
        skipped = False
        try:
            skipped = await self._process_retry_queue(admin_user_id)
        except Exception as e:  # noqa: BLE001 Do not catch blind exception
            # Log that there was an error so it does not raise and break the looping
            # call
            logger.warning(
                "Unexpected exception while processing the retry queue: %r", e
            )
        finally:
            # Because if the function was tried but short-cut out of, we don't want to
            # reset the flag or every other attempt will succeed. Defeats the purpose.
            if not skipped:
                self.retry_queue_process_running = False

    async def _process_retry_queue(self, admin_user_id: str | None = None) -> bool:
        """
        Iterate through both collections of types of ID. If an external ID is found,
        move it from the external_id queue to the members queue directly.

        Args:
            admin_user_id: An optional admin_user_id. Used for saving the retry queue
                snapshot. When not provided it will be attempted to be looked up. Should
                only not appear before managed rooms are created.

        Returns: A bool representing if this was a skipped run(because currently running)
        """
        # This should not happen in production, but during tests multiple copies of this
        # function were firing at once(13 of them!). Just enforce that should not happen
        if self.retry_queue_process_running:
            return True

        self.retry_queue_process_running = True

        async with self.retry_queue_lock:
            rooms_to_delete = set()
            for room_id, room_queue in self.retry_queue.rooms.items():
                # First find out if the room even still exists
                try:
                    await self._assert_room_exists(room_id)
                except NotFoundError:
                    # To avoid iterable changing size during processing, save this room_id to remove at the appropriate
                    # spot below, but otherwise spend no more time on it
                    rooms_to_delete.add(room_id)
                    continue

                # Collect all the various ID's for a given room, then can remove them all
                # at once at the end and avoid "dictionary changed size while iterating"
                external_ids_to_remove: set[str] = set()
                mxids_to_remove: set[str] = set()

                # Batch the call to get all the external user ids that may have been found
                external_id_to_mxids_mapping = (
                    await self.batch_convert_external_user_ids_to_matrix_user_ids(
                        room_queue.external_ids.keys()
                    )
                )

                for external_id, action_reason in room_queue.external_ids.items():
                    if external_id not in external_id_to_mxids_mapping:
                        # The mapping was still not found
                        action_reason.retry_count += 1
                        action_reason.latest_attempt_utc_ms = self.clock.time_msec()
                        continue
                    mxid = external_id_to_mxids_mapping[external_id]
                    room_queue.members[mxid] = action_reason
                    external_ids_to_remove.add(external_id)
                    logger.debug(
                        "Mapping for External User ID('%s') found, moving to members queue",
                        external_id,
                    )

                # This could raise, but it's unlikely. Trap it anyway, just in case
                try:
                    room_creator = await self.get_room_creator(room_id)
                except NotFoundError:
                    rooms_to_delete.add(room_id)
                    # May as well move on, no sense processing more of this room
                    continue

                for mxid, action_reason in room_queue.members.items():
                    if action_reason.is_removal is True:
                        returned_errors = await self.remove_users_from_room(
                            room_creator, [mxid], room_id
                        )
                    else:
                        returned_errors = await self.force_join_users_to_room(
                            room_id, [mxid], room_creator
                        )
                    if mxid not in returned_errors:
                        mxids_to_remove.add(mxid)
                    else:
                        self.retry_queue.rooms[room_id].members[mxid].retry_count += 1
                        self.retry_queue.rooms[room_id].members[
                            mxid
                        ].latest_attempt_utc_ms = self.clock.time_msec()

                # Remove no longer needed entries
                for external_id in external_ids_to_remove:
                    self.retry_queue.rooms[room_id].external_ids.pop(external_id, None)
                for mxid in mxids_to_remove:
                    self.retry_queue.rooms[room_id].members.pop(mxid, None)

            # Clean up now unused room entries that are empty or for rooms that were deleted
            retry_queue_copy = self.retry_queue.rooms.copy()
            for room_id, room_queue in retry_queue_copy.items():
                if not room_queue or room_id in rooms_to_delete:
                    self.retry_queue.rooms.pop(room_id)

            # Process some metric numbers
            num_of_rooms = len(self.retry_queue.rooms)
            distinct_mxids: set[str] = set()
            distinct_external_ids: set[str] = set()
            num_of_joins = 0
            num_of_leaves = 0
            for room_queue in self.retry_queue.rooms.values():
                distinct_external_ids.update(room_queue.external_ids.keys())
                distinct_mxids.update(room_queue.members.keys())
                for generic_user_id, reason in chain(
                    room_queue.external_ids.items(), room_queue.members.items()
                ):
                    if reason.is_removal:
                        num_of_leaves += 1
                    else:
                        num_of_joins += 1
                    if (
                        reason.retry_count
                        >= self.config.error_retry_queue_log_after_retry_count
                    ):
                        logger.warning(
                            "Excessive number of membership error retries detected: User='%s', retry_count=%d, time_in_queue(seconds)=%d",
                            generic_user_id,
                            reason.retry_count,
                            (reason.latest_attempt_utc_ms - reason.first_attempt_utc_ms)
                            / 1000,
                        )

            num_of_distinct_external_ids = len(distinct_external_ids)
            num_of_distinct_mxids = len(distinct_mxids)

            famedly_control_error_retry_queue_properties.labels(
                "number_of_rooms", self.server_name
            ).set(num_of_rooms)
            famedly_control_error_retry_queue_properties.labels(
                "distinct_external_user_ids", self.server_name
            ).set(num_of_distinct_external_ids)
            famedly_control_error_retry_queue_properties.labels(
                "distinct_mxids", self.server_name
            ).set(num_of_distinct_mxids)
            famedly_control_error_retry_queue_properties.labels(
                "joins", self.server_name
            ).set(num_of_joins)
            famedly_control_error_retry_queue_properties.labels(
                "leaves", self.server_name
            ).set(num_of_leaves)

            # The queue has probably changed. Save a snapshot
            if not admin_user_id:
                admin_user_id = await self.get_admin_user_id_from_sync_token()
            if admin_user_id:
                await self.save_retry_queue_snapshot(admin_user_id)

        return False


def get_membership_for_user(mxid: str, state_map: StateMap[EventBase]) -> str:
    """
    Helper to extract the membership of a given user based on the StateMap provided

    Args:
        mxid: The user to look up
        state_map: A prepared StateMap that should have the users membership included,
            if it existed

    Returns: A string of the membership value, defaulting to "leave" if the user was
        not in the room
    """
    membership_event = state_map.get((EventTypes.Member, mxid))
    if membership_event:
        # If the membership event is present, then this value should exist and not need
        # a fallback default
        return membership_event.content.get(
            EventContentFields.MEMBERSHIP, Membership.LEAVE
        )
    # Default for a room with the member having never been there is "leave"
    return Membership.LEAVE


def parse_missing_items(
    origin_collection: Collection[str], expected_collection: Iterable[str]
) -> list[str]:
    """
    Retrieve a list of things that are not in the origin collection based on the
    expected collection
    """
    # A Collection type was explicitly chosen for the origin_collection, as the way we
    # use it comes in as a "set-like" view *and* we need to be able to use the
    # __contains__() method on it. Iterables do not have that
    return [item for item in expected_collection if item not in origin_collection]
