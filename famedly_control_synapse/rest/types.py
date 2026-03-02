from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from synapse.api.constants import EventTypes, GuestAccess, Membership
from synapse.types import JsonDict

MANAGED_ROOM_TYPE = "de.famedly.managedRoom"


class CreationContent(BaseModel):
    """Pydantic model for CreationContent."""

    creator: str | None = None
    m_federate: Literal[False] = Field(
        default=False,
        alias="m.federate",
        description="Federation is disabled for managed rooms.",
    )
    predecessor: JsonDict | None = None
    room_version: str | None = (
        None  # The room version here is overridden. It is irrelevant which version comes here.
    )
    type: str | None = None

    model_config = ConfigDict(validate_by_name=True, validate_by_alias=True)


class PowerLevelEventContent(BaseModel):
    """Power level event content for overriding default power levels."""

    ban: int = 100
    events: dict[str, int] = Field(
        default_factory=lambda: {
            EventTypes.Name: 100,
            EventTypes.Topic: 100,
            EventTypes.PowerLevels: 100,
            EventTypes.JoinRules: 100,
            EventTypes.CanonicalAlias: 100,
            EventTypes.RoomAvatar: 100,
        }
    )
    events_default: int = 0
    invite: int = 100
    kick: int = 100
    redact: int = 100
    state_default: int = 100
    users: dict[str, int] = Field(default_factory=dict)
    users_default: int = 0
    notifications: dict[str, int] = Field(default_factory=dict)


class CreateManagedRoomRequest(BaseModel):
    """Request body for creating a managed room."""

    room_alias_name: str
    name: str
    topic: str | None = None
    creation_content: CreationContent = Field(default_factory=CreationContent)
    initial_state: list[JsonDict] = Field(
        default_factory=lambda: [
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
        ]
    )
    power_level_content_override: PowerLevelEventContent = Field(
        default_factory=PowerLevelEventContent
    )
    room_version: str | None = None
    groups: list[str]
    is_direct: Literal[False] = Field(
        default=False, description="is_direct cannot be enabled for managed rooms."
    )
    visibility: Literal["private"] = Field(
        default="private",
        description="Visibility is fixed to private for managed rooms.",
    )

    model_config = ConfigDict(extra="forbid")


class AssignGroupsToManagedRoomRequest(BaseModel):
    """Request body for assigning groups to a managed room."""

    groups: list[str]

    model_config = ConfigDict(extra="forbid")
