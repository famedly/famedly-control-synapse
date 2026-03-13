from unittest import TestCase

import pytest
from parameterized import parameterized
from pydantic import ValidationError
from synapse.api.constants import CREATOR_POWER_LEVEL, EventTypes

from famedly_control_synapse.rest.types import (
    CreateManagedRoomRequest,
    CreationContent,
    PowerLevelEventContent,
)


class TestCreationContent:
    def test_valid_creator_for_v10(self):
        content = CreationContent(creator="@alice:example.com")
        assert content.creator == "@alice:example.com"

    def test_m_federate_always_false(self):
        content = CreationContent()
        assert content.m_federate is False

        with pytest.raises(ValidationError):
            content = CreationContent(m_federate=True)


class TestPowerLevelEventContent:
    def test_valid_power_level_event_content(self):
        plc = PowerLevelEventContent(
            users={"@alice:example.com": 100, "@bob:example.com": 50}, users_default=0
        )
        assert plc.users["@alice:example.com"] == 100
        assert plc.users["@bob:example.com"] == 50
        assert plc.users_default == 0

    def test_invalid_users_type(self):
        with pytest.raises(ValidationError):
            PowerLevelEventContent(
                users=["@alice:example.com", "@bob:example.com"], users_default=0
            )


class TestCreateManagedRoomRequest(TestCase):
    """
    Exercise the various options in the shape of the CreateManagedRoomRequest, to make
    sure it behaves as expected.

    As notes:
        * It is not expected here that the creation_content.creator field is filled in, as
        this is overridden by Synapse at the time of room creation.
        * While testing that a room version is passed in and in the correct places, the
        default behavior is to use the default room version defined by the server, so it
        is None here and filled in during the request
    """

    @parameterized.expand([("10",), ("12",)])
    def test_default(self, room_version):
        """
        Test that a basic object is created with expected parameters
        """
        json_dict = {
            "room_alias_name": "testroom",
            "name": "Test Room",
            "room_version": room_version,
            "groups": ["testgroup"],
        }

        req = CreateManagedRoomRequest.model_validate(json_dict)
        assert req.room_alias_name == "testroom"
        assert req.name == "Test Room"
        assert req.room_version == room_version
        # This does not need to exist here. It will be overridden during the room
        # creation, so is not needed
        assert req.creation_content.room_version is None
        assert req.creation_content.creator is None
        assert req.initial_state == [
            {
                "content": {
                    "guest_access": "forbidden",
                },
                "state_key": "",
                "type": "m.room.guest_access",
            },
            {
                "content": {
                    "join_rule": "invite",
                },
                "state_key": "",
                "type": "m.room.join_rules",
            },
        ]
        assert req.is_direct is False
        assert req.visibility == "private"
        # At the time this object is created, the room creator is not present in room
        # v10, this is added at the time of the request being processed. Users should be
        # empty
        assert len(req.power_level_content_override.users) == 0
        # By default, 6 event types are included in the events object
        assert len(req.power_level_content_override.events) == 6
        self.assertDictEqual(
            req.power_level_content_override.events,
            {
                EventTypes.Name: 100,
                EventTypes.Topic: 100,
                EventTypes.PowerLevels: CREATOR_POWER_LEVEL - 1,
                EventTypes.JoinRules: CREATOR_POWER_LEVEL - 1,
                EventTypes.CanonicalAlias: 100,
                EventTypes.RoomAvatar: 100,
            },
        )
        assert req.power_level_content_override.ban == CREATOR_POWER_LEVEL - 1
        assert req.power_level_content_override.invite == CREATOR_POWER_LEVEL - 1
        assert req.power_level_content_override.kick == CREATOR_POWER_LEVEL - 1
        assert req.power_level_content_override.redact == 100
        assert req.power_level_content_override.events_default == 0
        assert req.power_level_content_override.users_default == 0
        assert req.power_level_content_override.state_default == 100

        # A single group was requested, make sure it is there
        assert "testgroup" in req.groups
        assert len(req.groups) == 1

    def test_with_none_powerlevel(self) -> None:
        """
        Test passing a None as the powerlevel override is prohibited
        """
        # Then try by setting as None. Expect a ValidationError
        room_config = {
            "room_alias_name": "testroom",
            "name": "Test Room",
            "power_level_content_override": None,
            "groups": ["testgroup"],
        }

        with pytest.raises(ValidationError):
            CreateManagedRoomRequest.model_validate(room_config)

    def test_with_empty_powerlevel(self):
        """
        Test that adding an empty power level object does not allow circumventing all
        power levels
        """
        # First try with an empty dict, should still get the defaults
        room_config = {
            "room_alias_name": "testroom",
            "name": "Test Room",
            "power_level_content_override": {},
            "groups": [],
        }

        req = CreateManagedRoomRequest.model_validate(room_config)
        assert len(req.power_level_content_override.users) == 0
        # By default, 6 event types are included in the events object
        assert len(req.power_level_content_override.events) == 6
        self.assertDictEqual(
            req.power_level_content_override.events,
            {
                EventTypes.Name: 100,
                EventTypes.Topic: 100,
                EventTypes.PowerLevels: CREATOR_POWER_LEVEL - 1,
                EventTypes.JoinRules: CREATOR_POWER_LEVEL - 1,
                EventTypes.CanonicalAlias: 100,
                EventTypes.RoomAvatar: 100,
            },
        )
        assert req.power_level_content_override.ban == CREATOR_POWER_LEVEL - 1
        assert req.power_level_content_override.invite == CREATOR_POWER_LEVEL - 1
        assert req.power_level_content_override.kick == CREATOR_POWER_LEVEL - 1
        assert req.power_level_content_override.redact == 100
        assert req.power_level_content_override.events_default == 0
        assert req.power_level_content_override.users_default == 0
        assert req.power_level_content_override.state_default == 100

    def test_with_user_powerlevel_override(self):
        """
        Test that adding a user override power level operates as expected
        """
        room_config = {
            "room_alias_name": "testroom",
            "name": "Test Room",
            "power_level_content_override": {"users": {"@alice:example.com": 100}},
            "groups": ["testgroup"],
        }

        req = CreateManagedRoomRequest.model_validate(room_config)
        assert req.power_level_content_override.users_default == 0
        assert "@alice:example.com" in req.power_level_content_override.users
        assert req.power_level_content_override.users["@alice:example.com"] == 100
        # At the time this object is created, the room creator is not present in room
        # v10, this is added at the time of the request being processed. There should be
        # only the one we asked for
        assert len(req.power_level_content_override.users) == 1
