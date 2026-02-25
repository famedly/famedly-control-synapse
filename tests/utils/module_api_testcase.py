# Copyright (C) 2020, 2024 Famedly
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
# from jwcrypto import jwe, jwk, jwt
import logging
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, ClassVar

from synapse.rest import admin
from synapse.rest.client import (
    account_data,
    login,
    notifications,
    presence,
    profile,
    room,
    room_upgrade_rest_servlet,
)
from synapse.server import HomeServer
from synapse.types import UserID, create_requester
from synapse.util.clock import Clock
from twisted.internet.testing import MemoryReactor

import tests.utils.homeserver_testcase as synapsetest
from famedly_control_synapse.types import CreateManagedRoomRequest

logger = logging.getLogger(__name__)
# ruff: noqa: E501
# We don't care about long lines in our testdata

if TYPE_CHECKING:

    from synapse.storage.databases.main import DataStore
    from synapse.storage.databases.main.room import RoomWorkerStore


class ModuleApiTestCase(synapsetest.HomeserverTestCase):
    server_name_for_this_server = "testserver.com"
    _room_counter = 0
    BASE_PATH = "/_famedlyControl/v1/managedRooms"
    LIST_PATH = BASE_PATH + "/rooms"
    CREATE_PATH = BASE_PATH + "/createRoom"

    @classmethod
    def setUpClass(cls):
        pass

    @classmethod
    def tearDownClass(cls):
        pass

    servlets: ClassVar[list] = [
        admin.register_servlets,
        account_data.register_servlets,
        login.register_servlets,
        room.register_servlets,
        room_upgrade_rest_servlet.register_servlets,
        presence.register_servlets,
        profile.register_servlets,
        notifications.register_servlets,
    ]

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        super().prepare(reactor, clock, homeserver)
        self.store: DataStore | RoomWorkerStore = homeserver.get_datastores().main
        self.storage_controllers = homeserver.get_storage_controllers()
        self.module_api = homeserver.get_module_api()
        self.event_creation_handler = homeserver.get_event_creation_handler()
        self.auth_handler = homeserver.get_auth_handler()
        self.creator = self.register_user("room_creator", "password", admin=True)
        self.creator_access_token = self.login("room_creator", "password")
        self.creator_token_id = self.get_success(
            self.hs.get_datastores().main.add_access_token_to_user(
                self.creator,
                "dummy",
                None,
                None,
            )
        )
        self.requester = create_requester(
            user_id=UserID.from_string(self.creator),
            access_token_id=self.creator_token_id,
        )
        self.invitee = self.register_user("invitee", "password")

    def default_config(self) -> dict[str, Any]:
        conf = super().default_config()
        if "modules" not in conf:
            conf["modules"] = [
                {
                    "module": "famedly_control_synapse.FamedlyControl",
                    "config": {
                        "title": "Famedly Control module",
                        "description": "Famedly Control module by Famedly",
                        "contact": "info@famedly.com",
                        "url": "http://dummy.test/famedlyControl",
                        "access_token": "dummy_token_for_testing",
                        "api_key": "dummy_api_key_for_testing",
                    },
                }
            ]
        return conf

    def _test_get_membership(
        self, room: str, members: list[str], expect_code: int = 200
    ) -> None:
        """Helper method to check the membership of a room.
        Returns 200 if the user is a member. If not, returns 403 accordingly."""
        for member in members:
            path = "/rooms/%s/state/m.room.member/%s" % (room, member)
            channel = self.make_request(
                "GET", path, access_token=self.creator_access_token
            )
            self.assertEqual(expect_code, channel.code)

    def _create_managed_room(
        self, name: str = "Test Room", groups: list[str] | None = None
    ) -> str:
        """Helper method to create a managed room with groups.
        At the moment requires mock for the get_group_members
        Returns room ID of the created room."""
        self._room_counter += 1
        config = CreateManagedRoomRequest(
            room_alias_name=f"test_room_{self._room_counter}",
            name=name,
            topic=f"Topic for {name}",
            groups=["test_group"],
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
