import pytest
from pydantic import ValidationError

from famedly_control_synapse.types import (
    CreateManagedRoomRequest,
    CreationContent,
    PowerLevelEventContent,
)


class TestCreationContent:
    def test_creator_required_for_v10(self):
        with pytest.raises(ValidationError):
            CreationContent(room_version="10")

    def test_valid_creator_for_v10(self):
        content = CreationContent(creator="@alice:example.com", room_version="10")
        assert content.creator == "@alice:example.com"

    def test_valid_no_creator_for_v11(self):
        content = CreationContent(room_version="11")
        assert content.room_version == "11"
        assert content.creator is None

    def test_invalid_creator_format(self):
        with pytest.raises(ValidationError):
            CreationContent(creator="alice", room_version="10")

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


class TestCreateManagedRoomRequest:
    def test_create_managed_room_v10_request_valid(self):
        # Valid request
        req = CreateManagedRoomRequest(
            room_alias_name="testroom",
            name="Test Room",
            creation_content=CreationContent(
                creator="@alice:example.com", room_version="10"
            ),
            power_level_content_override=PowerLevelEventContent(
                users={"@alice:example.com": 100}, users_default=0
            ),
        )
        assert req.room_alias_name == "testroom"
        assert req.creation_content.creator == "@alice:example.com"

    def test_create_managed_room_request_valid(self):
        # Valid request
        req = CreateManagedRoomRequest(
            room_alias_name="testroom",
            name="Test Room",
            creation_content=CreationContent(),
            power_level_content_override=PowerLevelEventContent(),
        )
        assert req.room_alias_name == "testroom"
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

    def test_create_managed_room_request_invalid_room_alias(self):
        # Invalid alias
        with pytest.raises(ValidationError):
            CreateManagedRoomRequest(room_alias_name="Invalid Alias!")
