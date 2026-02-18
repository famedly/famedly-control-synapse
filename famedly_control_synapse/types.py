import re
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from synapse.api.constants import EventTypes, GuestAccess, Membership
from synapse.types import JsonDict


class CreationContent(BaseModel):
    """Pydantic model for CreationContent."""

    model_config = ConfigDict(validate_by_name=True, validate_by_alias=True)

    creator: Optional[str] = Field(
        default=None,
        description="The user_id of the room creator. Required for, and only present"
        " in, room versions 1 - 10. Starting with room version 11 the event `sender` "
        "should be used instead.",
    )
    # TODO: Test case of the creator field

    m_federate: Literal[False] = Field(
        default=False,
        alias="m.federate",
        description="Federation is disabled for managed rooms.",
    )

    predecessor: Optional[JsonDict] = Field(
        default=None,
        description="A reference to the room this room replaces, if the previous room was upgraded.",
    )

    room_version: str = Field(
        default="12",  # TODO: use config
        description="The version of the room. Defaults to '12' if the key does not exist.",
    )

    type: Optional[str] = Field(
        default=None,
        description="Optional room type to denote a room's intended function outside "
        "of traditional conversation. Unspecified room types are possible "
        "using Namespaced Identifiers.",
    )

    @field_validator("creator")
    @classmethod
    def validate_creator(cls, v: Optional[str]) -> Optional[str]:
        """Validate that creator is a valid user ID format."""
        if v is not None and (not v.startswith("@") or ":" not in v):
            raise ValueError(f"Invalid user ID format: {v}")
        return v

    @model_validator(mode="after")
    def validate_creator_for_room_version(self) -> "CreationContent":
        """Validate that creator is specified for room versions <= 10."""
        try:
            if int(self.room_version) <= 10 and self.creator is None:
                raise ValueError(
                    f"creator must be specified for room version {self.room_version} (<= 10)"
                )
        except ValueError as e:
            # Re-raise if it's our validation error, otherwise skip validation for non-numeric versions
            if "creator must be specified" in str(e):
                raise
        return self


class PowerLevelEventContent(BaseModel):
    """Power level event content for overriding default power levels."""

    ban: Optional[int] = Field(
        default=100, description="The level required to ban a user"
    )
    events: Optional[Dict[str, int]] = Field(
        default={
            EventTypes.Name: 100,
            EventTypes.Topic: 100,
            EventTypes.PowerLevels: 100,
            EventTypes.JoinRules: 100,
            EventTypes.CanonicalAlias: 100,
            EventTypes.RoomAvatar: 100,
        },
        description="The level required to send specific event types",
    )
    events_default: Optional[int] = Field(
        default=0, description="The default level required to send message events"
    )
    invite: Optional[int] = Field(
        default=100, description="The level required to invite a user to the room"
    )
    kick: Optional[int] = Field(
        default=100, description="The level required to kick a user"
    )
    redact: Optional[int] = Field(
        default=100, description="The level required to redact an event"
    )
    state_default: Optional[int] = Field(
        default=100, description="The default level required to send state events"
    )
    users: Optional[Dict[str, int]] = Field(
        default={}, description="The power levels for specific users"
    )
    users_default: Optional[int] = Field(
        default=0, description="The default power level for users in the room"
    )
    notifications: Optional[Dict[str, int]] = Field(
        default={},
        description="The power level requirements for specific notification types",
    )


class CreateManagedRoomRequest(BaseModel):
    """Request body for creating a managed room."""

    room_alias_name: Optional[str] = Field(
        default=None,
        description="The desired room alias local part. If this is included, a room"
        " alias will be created and mapped to the newly created room.",
    )
    name: Optional[str] = Field(
        default=None,
        description="If this is included, an m.room.name event will be sent into the "
        "room to indicate the name for the room.",
    )
    topic: Optional[str] = Field(
        default=None,
        description="If this is included, an m.room.topic event with a text/plain "
        "mimetype will be sent into the room to indicate the topic for the room.",
    )

    creation_content: Optional[CreationContent] = Field(
        default=CreationContent(),
        description="Extra keys, such as m.federate, to be added to the content of the "
        "m.room.create event. The server will overwrite the following keys: creator, "
        "room_version.",
    )

    initial_state: Optional[List[JsonDict]] = Field(
        default=[
            {
                "type": EventTypes.GuestAccess,
                "state_key": "",
                "content": {"guest_access": GuestAccess.FORBIDDEN},
            },
            {
                "type": EventTypes.JoinRules,
                "state_key": "",
                "content": {"join_rule": Membership.INVITE},
            },
        ],
        description="A list of state events to set in the new room. This allows the "
        "user to override the default state events set in the new room.",
    )

    power_level_content_override: Optional[PowerLevelEventContent] = Field(
        default=PowerLevelEventContent(),
        description="The power level content to override in the default power level "
        "event. This object is applied on top of the generated m.room.power_levels"
        " event content.",
    )

    room_version: Optional[str] = Field(
        default="12",
        description="The room version to set for the room. If not provided, the "
        "homeserver is to use its configured default.",
    )

    groups: Optional[List[str]] = Field(
        default=None,
        description="List of group IDs to be stored in the de.famedly.managedRoom"
        " account data.",
    )

    is_direct: Literal[False] = Field(
        default=False, description="is_direct cannot be enabled for managed rooms."
    )

    visibility: Literal["private"] = Field(
        default="private",
        description="Visibility is fixed to private for managed rooms.",
    )

    @field_validator("room_alias_name")
    @classmethod
    def validate_room_alias(cls, v: Optional[str]) -> Optional[str]:
        """Validate room alias name format."""
        if v is not None:
            # Room alias local part should not contain invalid characters
            if not re.match(r"^[a-z0-9._=-]+$", v.lower()):
                raise ValueError(f"Invalid room alias name format: {v}")
        return v
