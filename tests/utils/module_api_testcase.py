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

logger = logging.getLogger(__name__)
# ruff: noqa: E501
# We don't care about long lines in our testdata

if TYPE_CHECKING:

    from synapse.storage.databases.main import DataStore
    from synapse.storage.databases.main.room import RoomWorkerStore


class ModuleApiTestCase(synapsetest.HomeserverTestCase):
    server_name_for_this_server = "testserver.com"

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
                    },
                }
            ]
        return conf
