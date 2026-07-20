from http import HTTPStatus
from typing import Any
from unittest.mock import AsyncMock, patch

from synapse.api.constants import EventTypes
from synapse.server import HomeServer
from synapse.types import StateMap
from synapse.types.state import StateFilter
from synapse.util.async_helpers import DeferredEvent
from synapse.util.clock import Clock
from twisted.internet import defer
from twisted.internet.testing import MemoryReactor

from famedly_control_synapse import FamedlyControl
from famedly_control_synapse.client import (
    DiffRecord,
    ManyGroupsDiffResponse,
    MembershipAction,
)
from famedly_control_synapse.types import MANAGED_ROOM_TYPE
from tests.utils.module_api_testcase import ModuleApiTestCase


@patch(
    "famedly_control_synapse.client.FamedlyControlClient.get_all_groups_diffs",
    new_callable=AsyncMock,
)
@patch(
    "famedly_control_synapse.client.FamedlyControlClient.get_group_members",
    new_callable=AsyncMock,
)
class TestShutdownRoomCase(ModuleApiTestCase):
    """
    Test that shutting down a room with the Synapse Admin API for deleting rooms acts as expected. It was decided to use
    v1 of the API for this as it is synchronous. v2 uses the same code paths but is asynchronous and is harder to test.

    The point of these tests are twofold:
    * Make sure the delete part of the Admin API continues to work easily without extra workarounds
    * Make sure the module does its own clean up to not leave old stray/stale data lying around forever.
    """

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        super().prepare(reactor, clock, homeserver)

        self.default_room_version = (
            self.hs.config.server.default_room_version.identifier
        )
        self.room_control: FamedlyControl = self.hs.room_control

        self.sys_admin_user = self.register_user("sysadmin", "password", admin=True)
        self.sys_admin_user_token = self.login("sysadmin", "password")

        self.reg_user = self.register_user("regular", "password")
        self.reg_user_token = self.login("regular", "password")

        self.group_id = "test_group"

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        config["modules"][0]["config"]["error_retry_queue_enabled"] = True
        config["modules"][0]["config"]["sync_enabled"] = False
        return config

    def _get_state_map_of_room(self, room_id: str) -> StateMap:
        return self.get_success(
            self.hs.get_storage_controllers().state.get_current_state(
                room_id, StateFilter.all()
            )
        )

    def _create_test_room(self) -> str:
        """Create simple managed room. Make sure your 'get_group_members' mock is prepared in advance"""
        config = {
            "room_alias_name": "test_room_alias",
            "name": "Test Room",
            "topic": "This is a test room",
            "groups": [self.group_id],
        }
        channel = self.make_request(
            method="POST",
            path=self.CREATE_PATH,
            content=config,
            access_token=self.creator_access_token,
            shorthand=False,
        )

        assert channel.code == HTTPStatus.OK, channel.result
        assert "room_id" in channel.json_body, "Response should contain room_id"
        assert "groups" in channel.json_body, "Response should contain groups"
        assert channel.json_body["groups"] == [self.group_id]
        return channel.json_body["room_id"]

    def test_shutdown_room(
        self, mock_get_group_members, mock_get_all_groups_diffs
    ) -> None:
        """
        Test that shutting down a room works as expected with no outside influence(like the retry queue or sync loop)
        """
        mock_get_group_members.return_value = [self.sys_admin_user, self.reg_user]
        # Have a managed room ready, 2 users other than the room creator
        room_id = self._create_test_room()

        # Check if the room has the correct configuration. Should be 3 members: the room creator, and the two members of
        # the group.
        state_map = self._get_state_map_of_room(room_id)
        for user in [self.creator, self.sys_admin_user, self.reg_user]:
            assert (
                EventTypes.Member,
                user,
            ) in state_map, f"expected user missing from state_map, missing={user}"

        # Check that the micro-state for this room has a "groups" entry, this is the key the room is established and
        # what we will look for later to make sure is cleaned up
        a_data = self.get_success(
            self.store.get_account_data_for_room_and_type(
                self.creator, room_id, MANAGED_ROOM_TYPE
            )
        )
        assert a_data is not None
        assert "groups" in a_data
        assert len(a_data["groups"]) > 0

        # Delete the room using the admin api
        channel = self.make_request(
            "DELETE",
            f"/_synapse/admin/v1/rooms/{room_id}",
            {},
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        assert (
            "kicked_users" in channel.json_body
        ), "Response should contain kicked_users"
        assert (
            len(channel.json_body["kicked_users"]) == 3
        ), "Should have kicked_users count be 3"

        # Check that the micro-state for this room has been cleaned up
        a_data = self.get_success(
            self.store.get_account_data_for_room_and_type(
                self.creator, room_id, MANAGED_ROOM_TYPE
            )
        )
        assert a_data is None

    def test_shutdown_room_with_retry_queue(
        self, mock_get_group_members, mock_get_all_groups_diffs
    ) -> None:
        """
        Test that shutting down a room works as expected with retry queue entry. This is important not only for tidiness
        on the database, but also so a deleted room does not accidentally get re-created/joined
        """
        mock_get_group_members.return_value = [
            self.sys_admin_user,
            "@rando-non-existent:test",
        ]
        # Have a managed room ready, 1 extra users and one in the retry queue
        room_id = self._create_test_room()

        # Check if the room has the correct configuration. Should be 2 members: the room creator, and the one member of
        # the group. The other user should not be joined
        state_map = self._get_state_map_of_room(room_id)
        for user in [self.creator, self.sys_admin_user]:
            assert state_map[
                EventTypes.Member, user
            ], f"expected user missing from state_map, missing={user}"
        assert (
            EventTypes.Member,
            "@rando-non-existent:test",
        ) not in state_map, "rando should not be in the room"

        # Since a member did not exist, there should be a basic room entry in the retry queue
        assert (
            room_id in self.room_control.room_handler.retry_queue.rooms
        ), "Room ID was not found in retry_queue"

        # Delete the room using the admin api
        channel = self.make_request(
            "DELETE",
            f"/_synapse/admin/v1/rooms/{room_id}",
            {},
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        assert (
            "kicked_users" in channel.json_body
        ), "Response should contain kicked_users"
        assert (
            len(channel.json_body["kicked_users"]) == 2
        ), "Should have kicked_users count be 2"

        # Have to poke the retry queue to do its thing, or it won't drop the old entries
        self.get_success(
            self.room_control.room_handler.process_retry_queue(self.creator)
        )
        # the room should no longer be in the retry queue
        assert (
            room_id not in self.room_control.room_handler.retry_queue.rooms
        ), "Room ID was found in retry_queue, but should have been removed"

    async def test_shutdown_room_with_sync_loop(
        self, mock_get_group_members, mock_get_all_groups_diffs
    ) -> None:
        """
        Test that shutting down a room works as expected with sync loop entry for a relevant group. Just like for the
        test with the retry loop, this can be a problem when a dangling leftover user is trying to be joined to a room
        that does not exist anymore. Or if the user is attempted to be joined to a 'half-deleted' room.

        Note: Even though this test is 'async', awaits do not always work on the test reactor predictably. There will be
          occasions where the reactor does have to be periodically poked at to advance time and run event loop
          iterations.
        """
        mock_get_group_members.return_value = [self.sys_admin_user]
        # have a managed room ready, 1 extra user
        room_id = self._create_test_room()

        # We will be poking the sync loop processor to run, but the sync loop itself will not be started. Establish the
        # sync token and the correct user in advance, since creating the room initializes the token in the database, but
        # not actually load it into the syncer
        assert self.room_control.syncer._sync_token_user_id is None
        assert self.room_control.syncer._sync_token is None
        entry = self.get_success(self.room_control.repository.get_sync_token_entry())
        assert entry is not None
        (
            self.room_control.syncer._sync_token_user_id,
            self.room_control.syncer._sync_token,
        ) = entry
        assert self.room_control.syncer._sync_token_user_id == self.creator
        assert self.room_control.syncer._sync_token is None

        # Check if the room has the correct configuration
        state_map = self._get_state_map_of_room(room_id)

        # Should be 2 members: the room creator, and the one member of the group. "@regular:test" should not be in the
        # room yet
        for user in [self.creator, self.sys_admin_user]:
            assert state_map[
                EventTypes.Member, user
            ], f"expected user missing from state_map, missing={user}"
        assert (
            EventTypes.Member,
            self.reg_user,
        ) not in state_map, f"{self.reg_user} should not be in the room yet"

        # For our test, we are going to "wedge" the sync loop approximately halfway through, at the point it begins to
        # apply the diffs for all rooms. At that spot the room will be deleted, then applying diffs process will
        # continue. We expect the membership join to fail(as it will register as an unknown room)
        mock_get_group_members.return_value = [self.sys_admin_user, self.reg_user]
        mock_get_all_groups_diffs.return_value = ManyGroupsDiffResponse(
            next_sync="2",
            data={
                self.group_id: [
                    DiffRecord(user_id=self.reg_user, action=MembershipAction.ADD),
                ],
            },
        )
        # Grab a reference to the original function, as there will be no other access to it after the mock is in place.
        orig_apply_diffs = self.room_control.syncer._apply_diffs

        # Just like asyncio.Event, a DeferredEvent(which is a Synapse construct) will cause an async process to 'pause'
        # until it is signaled to continue. We will have two for signaling and gating our mock
        reached_wedge_signal = DeferredEvent(self.clock)
        resume_func_signal = DeferredEvent(self.clock)

        async def wrapped_apply_diffs(*args, **kwargs) -> None:
            # Unblock the first wedge, signaling we are inside the actual function now
            reached_wedge_signal.set()
            # Pause here and wait
            await resume_func_signal.wait(1.0)
            # Good, lets finish up the sync process
            return await orig_apply_diffs(*args, **kwargs)

        with patch(
            "famedly_control_synapse.sync.GroupMembershipSyncer._apply_diffs",
            new=AsyncMock(side_effect=wrapped_apply_diffs),
        ):
            # Begin the call to _process_sync(), then let the reactor run for a bit to get to the wedge. We assign this
            # to a deferred, which causes it to be scheduled on the reactor. It will get stuck because of the patch
            # above, which is what we want
            patched_sync_process = defer.maybeDeferred(
                self.room_control.syncer._process_sync
            )
            # Poke the reactor, to make sure it gets started
            self.reactor.advance(0.001)

            # _process_sync() has ran for a little while so we cause the process to stop at _apply_diffs(). Timeouts
            # here are arbitrary, as time is fake in tests. The 'reached_wedge_signal' may not strictly be necessary,
            # but does give a clear indication that progress has been made where it is expected.
            await reached_wedge_signal.wait(1.0)

            # Delete the room using the Admin API
            channel = self.make_request(
                "DELETE",
                f"/_synapse/admin/v1/rooms/{room_id}",
                {},
                access_token=self.creator_access_token,
                shorthand=False,
            )
            assert channel.code == HTTPStatus.OK, channel.result
            assert (
                "kicked_users" in channel.json_body
            ), "Response should contain kicked_users"
            # The sync loop paused before it could add the third member to the room, so 2 is what we are looking for
            assert (
                len(channel.json_body["kicked_users"]) == 2
            ), "Should have kicked_users count be 2"

            # Kick the _process_sync() we stopped at _apply_diffs()
            resume_func_signal.set()
            # The reactor needs a little poke to finish the signal resolve to unwedge the sync process
            self.reactor.advance(0.001)
            await patched_sync_process

        # The sync process should be finished, but: is our room actually deleted?
        # An unknown room will show as None here instead of the tuple
        room = self.get_success(self.store.get_room(room_id))
        assert room is None

        # check the state map too
        state_map = self._get_state_map_of_room(room_id)
        for user in [self.creator, self.sys_admin_user, self.reg_user]:
            assert (
                EventTypes.Member,
                user,
            ) not in state_map, f"{user} should not be in the room"

        assert room_id not in self.room_control.room_handler.retry_queue.rooms

    async def test_shutdown_room_during_group_assignment(
        self, mock_get_group_members, mock_get_all_groups_diffs
    ) -> None:
        """
        Test the scenario for a competing assign groups call with a delete room call.
        """
        # This test will start with one member in a group, assign two new members to that group using the /assignGroups
        # endpoint and simultaneously delete the room. One member will exist and the other will not. To test this,
        # create a wedge in the /assignGroups call so that after the group membership is fetched it pauses to allow the
        # room deletion to run to completion, the resume assigning the group. The expectation is that the room is not
        # reopened, no members actually are joined to the room, a 404 is returned and no dangling artifacts are left in
        # the retry queue.

        mock_get_group_members.return_value = [self.sys_admin_user]
        # Have a managed room ready, 1 user other than the room creator
        room_id = self._create_test_room()

        # Check if the room has the correct configuration. Should be 2 members: the room creator, and the one member of
        # the group.
        state_map = self._get_state_map_of_room(room_id)
        for user in [self.creator, self.sys_admin_user]:
            assert (
                EventTypes.Member,
                user,
            ) in state_map, f"expected user missing from state_map, missing={user}"

        assert (
            room_id not in self.room_control.room_handler.retry_queue.rooms
        ), "Room ID was found in retry_queue"

        # For our test, we are going to "wedge" the assign groups endpoint at the point it begins to assign the members
        # of the group. The room will have already been checked for existence, and the membership of the group will have
        # already been determined.
        # Then, the room will be deleted and the assign groups process will be allowed to continue. We expect the
        # membership join to fail(as it will register as an unknown room)
        nonexistent_user = f"@rando-non-existent:{self.server_name_for_this_server}"
        mock_get_group_members.return_value = [
            self.sys_admin_user,
            self.reg_user,
            nonexistent_user,
        ]

        # Grab a reference to the original function, as there will be no other access to it after the mock is in place.
        orig_assign_groups_to_room = (
            self.room_control.room_handler.assign_groups_to_room
        )

        # Just like asyncio.Event, a DeferredEvent(which is a Synapse construct) will cause an async process to 'pause'
        # until it is signaled to continue. We will have two for signaling and gating our mock
        reached_wedge_signal = DeferredEvent(self.clock)
        resume_func_signal = DeferredEvent(self.clock)

        async def wrapped_assign_groups_to_room(*args, **kwargs) -> None:
            # Unblock the first wedge, signaling we are inside the actual function now
            reached_wedge_signal.set()
            # Pause here and wait. The timeout value is arbitrary because tests use fake time
            await resume_func_signal.wait(1.0)
            # Good, lets finish up the group assignment process
            return await orig_assign_groups_to_room(*args, **kwargs)

        with patch(
            "famedly_control_synapse.room_handler.ManagedRoomHandler.assign_groups_to_room",
            new=AsyncMock(side_effect=wrapped_assign_groups_to_room),
        ):

            # call /assignGroups, which will pause at the appropriate spot
            assign_grp_req_channel = self.make_request(
                method="POST",
                path=self.BASE_PATH + f"/{room_id}/groups",
                content={"groups": [self.group_id]},
                access_token=self.creator_access_token,
                shorthand=False,
                # note the await result here being False so that this does not pin the test in the wrong spot, otherwise
                # the request hits the timeout and that certainly doesn't help at all
                await_result=False,
            )
            # give the reactor a little push, so everything can get caught up and hit the wedge
            self.reactor.advance(0.001)

            # The assign groups call should have gotten to the 'reached_wedge_signal' by now and gone ahead and set it.
            # The below wait is just a gatekeeper, in case that is not true. Right after this has processed, the
            # assigned groups call should be paused, which is the sign it is time to delete the room.
            await reached_wedge_signal.wait(1.0)

            # Delete the room using the admin api
            delete_req_channel = self.make_request(
                "DELETE",
                f"/_synapse/admin/v1/rooms/{room_id}",
                {},
                access_token=self.creator_access_token,
                shorthand=False,
            )

            assert delete_req_channel.code == HTTPStatus.OK, delete_req_channel.result
            assert (
                "kicked_users" in delete_req_channel.json_body
            ), "Response should contain kicked_users"
            assert (
                len(delete_req_channel.json_body["kicked_users"]) == 2
            ), "Should have kicked_users count be 2"

            # Kick the assign groups process we stopped earlier
            resume_func_signal.set()
            # Let the request for the assign groups finish up
            assign_grp_req_channel.await_result()
            assert (
                assign_grp_req_channel.code == HTTPStatus.NOT_FOUND
            ), assign_grp_req_channel.result

        # The sync process should be finished, but: is our room actually deleted?
        # An unknown room will show as None here instead of the tuple
        room = self.get_success(self.store.get_room(room_id))
        assert room is None, "Room should have been deleted"

        # check the state map too
        state_map = self._get_state_map_of_room(room_id)
        for user in [
            self.creator,
            self.sys_admin_user,
            self.reg_user,
            nonexistent_user,
        ]:
            assert (
                EventTypes.Member,
                user,
            ) not in state_map, f"{user} was found in the room"

        assert (
            room_id not in self.room_control.room_handler.retry_queue.rooms
        ), "Room ID was found in retry_queue"
