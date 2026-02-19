from http import HTTPStatus

from synapse.api.constants import (
    EventTypes,
)
from synapse.server import HomeServer
from synapse.types.state import StateFilter
from synapse.util.clock import Clock
from twisted.internet.testing import MemoryReactor

from famedly_control_synapse.types import CreateManagedRoomRequest
from tests.utils.module_api_testcase import ModuleApiTestCase


class TestManagedRoomCreation(ModuleApiTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer):
        super().prepare(reactor, clock, homeserver)

    def room_config(self, group_id: str | None = None):
        config = CreateManagedRoomRequest(
            room_alias_name="test_room_alias",
            name="Test Room",
            topic="This is a test room",
        )
        if group_id:
            config.groups = [group_id]

        # logging.error("Room Config Object %s", config)

        return config.model_dump()

    def test_room_creation_success(self) -> None:
        """Tests that managed room creation returns the expected response"""
        channel = self.make_request(
            method="POST",
            path="/_famedlyControl/v1/managedRooms/createRoom",
            content=self.room_config(),
            access_token=self.creator_access_token,
            shorthand=False,
        )

        assert channel.code == HTTPStatus.OK, channel.result
        assert "room_id" in channel.json_body, "Response should contain room_id"

        # Check if the room has the correct configuration (join rules, power levels, etc.)
        room_id = channel.json_body["room_id"]

        # TODO: do creator test with room v10 and below
        # channel = self.make_request(
        #     method="GET",
        #     path=f"/_matrix/client/v3/rooms/{room_id}/state/m.room.create",
        #     access_token=self.creator_access_token,
        # )
        # assert channel.code == HTTPStatus.OK, channel.result
        # logging.error("Guest Access State Event: %s", channel.json_body)
        # assert channel.json_body["creator"] == self.creator, channel.json_body["creator"]

        channel = self.make_request(
            method="GET",
            path=f"/_matrix/client/v3/rooms/{room_id}/state/m.room.join_rules",
            access_token=self.creator_access_token,
        )
        assert channel.code == HTTPStatus.OK, channel.result

        assert channel.json_body["join_rule"] == "invite", channel.json_body[
            "join_rule"
        ]

        channel = self.make_request(
            method="GET",
            path=f"/_matrix/client/v3/rooms/{room_id}/state/m.room.guest_access",
            access_token=self.creator_access_token,
        )
        assert channel.code == HTTPStatus.OK, channel.result

        assert channel.json_body["guest_access"] == "forbidden", channel.json_body[
            "guest_access"
        ]

        # room_data = self.get_success(
        #     self.hs.get_message_handler().get_room_data(
        #         self.requester, room_id, "m.room.join_rules", ""
        #     )
        # )
        # logging.error("room_data: %s", room_data.items())

        room_stats = self.get_success(
            self.hs.get_storage_controllers().state.get_current_state(
                room_id,
                StateFilter.from_types(
                    [(EventTypes.GuestAccess, None), (EventTypes.JoinRules, None)]
                ),
            )
        )
        # logging.error("room_stats: %s", room_stats.items())
        # logging.error("JoinRules: %s", dict(room_stats[EventTypes.JoinRules, ""]))

        assert room_stats[EventTypes.JoinRules, ""].content == {
            "join_rule": "invite"
        }, "Join rules should be set to invite"
        assert room_stats[EventTypes.GuestAccess, ""].content == {
            "guest_access": "forbidden"
        }, "Guest access should be set to forbidden"

    def invalid_room_config(self, group_id: str | None = None):
        config = {
            "room_alias_name": "test_room_alias",
            "name": "Test Room",
            "topic": "This is a test room",
            "3pid_invites": [],
        }
        if group_id:
            config["groups"] = [group_id]
        # logging.error("Room Config Object %s", config)
        return config

    # def test_room_creation_invalid_body(self) -> None:
    #     """Tests that invalid request body returns a 400 error with an error message"""
    #     channel = self.make_request(
    #         method="POST",
    #         path="/_famedlyControl/v1/managedRooms/createRoom",
    #         content=self.invalid_room_config(),
    #         access_token=self.creator_access_token,
    #         shorthand=False,
    #     )

    #     assert channel.code == HTTPStatus.BAD_REQUEST, channel.result
    #     assert "error" in channel.json_body, "Response should contain an error message"

    # TODO: Add tests for the room creation endpoint, including:
    # POST with invalid body returns 400 with an error message
    # POST with missing required fields returns 400 with an error message
    # POST with invalid creator format returns 400 with an error message or creator missing for room version <= 10
    # POST with different room versions
    # POST creates a room with the different and acceptable configuration (join rules, power levels, etc.)
    # Check if the created room has the correct account data event with the groups field populated correctly


class TestListManagedRooms(ModuleApiTestCase):
    LIST_PATH = "/_famedlyControl/v1/managedRooms/rooms"
    CREATE_PATH = "/_famedlyControl/v1/managedRooms/createRoom"

    def prepare(self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer):
        super().prepare(reactor, clock, homeserver)
        self.non_admin = self.register_user("non_admin", "password", admin=False)
        self.non_admin_token = self.login("non_admin", "password")
        self.account_data_handler = homeserver.get_account_data_handler()

    _room_counter = 0

    def _create_managed_room(
        self, name: str = "Test Room", groups: list[str] | None = None
    ) -> str:
        TestListManagedRooms._room_counter += 1
        config = CreateManagedRoomRequest(
            room_alias_name=f"test_room_{TestListManagedRooms._room_counter}",
            name=name,
            topic=f"Topic for {name}",
        )
        if groups:
            config.groups = groups
        channel = self.make_request(
            method="POST",
            path=self.CREATE_PATH,
            content=config.model_dump(),
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        return channel.json_body["room_id"]

    def test_list_requires_admin(self) -> None:
        """Non-admin users should get a 403."""
        channel = self.make_request(
            method="GET",
            path=self.LIST_PATH,
            access_token=self.non_admin_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.FORBIDDEN, channel.result

    def test_list_requires_auth(self) -> None:
        """Unauthenticated requests should get a 401."""
        channel = self.make_request(
            method="GET",
            path=self.LIST_PATH,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.UNAUTHORIZED, channel.result

    def test_list_empty(self) -> None:
        """Listing when no managed rooms exist should return empty chunk."""
        channel = self.make_request(
            method="GET",
            path=self.LIST_PATH,
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        assert channel.json_body["chunk"] == []
        assert channel.json_body["total_room_count_estimate"] == 0

    def test_list_returns_managed_rooms(self) -> None:
        """Created managed rooms should appear in the listing."""
        room_id = self._create_managed_room(
            name="Listed Room", groups=["group1", "group2"]
        )

        channel = self.make_request(
            method="GET",
            path=self.LIST_PATH,
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        assert channel.json_body["total_room_count_estimate"] == 1
        assert len(channel.json_body["chunk"]) == 1

        room_chunk = channel.json_body["chunk"][0]
        assert room_chunk["room_id"] == room_id
        assert room_chunk["de.famedly.managedRoom"]["groups"] == [
            "group1",
            "group2",
        ]

    def test_list_pagination(self) -> None:
        """Pagination should work with from and limit params."""
        room_ids = []
        for i in range(3):
            room_ids.append(self._create_managed_room(name=f"Room {i}"))

        # Get first page with limit 2
        channel = self.make_request(
            method="GET",
            path=f"{self.LIST_PATH}?limit=2",
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        assert len(channel.json_body["chunk"]) == 2
        assert channel.json_body["total_room_count_estimate"] == 3
        assert "next_batch" in channel.json_body
        assert "prev_batch" not in channel.json_body

        # Get second page
        next_batch = channel.json_body["next_batch"]
        channel = self.make_request(
            method="GET",
            path=f"{self.LIST_PATH}?from={next_batch}&limit=2",
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        assert len(channel.json_body["chunk"]) == 1
        assert "next_batch" not in channel.json_body
        assert "prev_batch" in channel.json_body

    def test_list_no_duplicates_with_multiple_users(self) -> None:
        """A room with account data from multiple users should appear once
        with all groups merged."""
        from famedly_control_synapse.types import MANAGED_ROOM_TYPE

        room_id = self._create_managed_room(name="Shared Room", groups=["group_a"])

        # Register extra users with overlapping groups
        user_groups = [
            ["group_a", "group_b"],  # overlaps with creator's group_a
            ["group_b", "group_c"],  # overlaps with previous user's group_b
            ["group_a", "group_c"],  # overlaps with both creator and user above
        ]
        for i, groups in enumerate(user_groups):
            user = self.register_user(f"extra_user_{i}", "password", admin=False)
            self.get_success(
                self.account_data_handler.add_account_data_to_room(
                    user,
                    room_id,
                    MANAGED_ROOM_TYPE,
                    {"groups": groups},
                )
            )

        channel = self.make_request(
            method="GET",
            path=self.LIST_PATH,
            access_token=self.creator_access_token,
            shorthand=False,
        )
        assert channel.code == HTTPStatus.OK, channel.result
        assert (
            channel.json_body["total_room_count_estimate"] == 1
        ), f"Expected 1 room but count was {channel.json_body['total_room_count_estimate']}"
        assert (
            len(channel.json_body["chunk"]) == 1
        ), f"Expected 1 entry in chunk but got {len(channel.json_body['chunk'])}"

        result_groups = channel.json_body["chunk"][0]["de.famedly.managedRoom"][
            "groups"
        ]
        assert sorted(result_groups) == [
            "group_a",
            "group_b",
            "group_c",
        ], f"Expected deduplicated merged groups, got {result_groups}"
