from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core.core_schema import ValidationInfo
from synapse.api.constants import (
    CREATOR_POWER_LEVEL,
    EventTypes,
    GuestAccess,
    Membership,
)
from synapse.types import JsonDict
from typing_extensions import Self

MANAGED_ROOM_TYPE = "de.famedly.managedRoom"
SYNC_TOKEN_TYPE = "de.famedly.roomControl.lastSyncToken.v1"


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

    # A full model validator is below to verify that membership action power levels can
    # not be overridden
    ban: int = CREATOR_POWER_LEVEL - 1
    events: dict[str, int] = Field(
        default_factory=lambda: {
            EventTypes.Name: 100,
            EventTypes.Topic: 100,
            EventTypes.PowerLevels: CREATOR_POWER_LEVEL - 1,
            EventTypes.JoinRules: CREATOR_POWER_LEVEL - 1,
            EventTypes.CanonicalAlias: 100,
            EventTypes.RoomAvatar: 100,
        }
    )
    events_default: int = 0
    invite: int = CREATOR_POWER_LEVEL - 1
    kick: int = CREATOR_POWER_LEVEL - 1
    redact: int = 100
    state_default: int = 100
    users: dict[str, int] = Field(default_factory=dict)
    users_default: int = 0
    notifications: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_membership_actions(self) -> Self:
        """
        Ensure that the membership action power levels can not be overridden
        """
        for action in ("ban", "invite", "kick"):
            action_value = getattr(self, action)
            if action_value < CREATOR_POWER_LEVEL - 1:
                raise ValueError(
                    f"Membership action('{action}') power level can not be overridden"
                )
        return self


class CreateManagedRoomRequest(BaseModel):
    """Request body for creating a managed room."""

    room_alias_name: str
    name: str
    topic: str | None = None
    creation_content: CreationContent = Field(default_factory=CreationContent)
    # initial_state has a validator below
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

    @field_validator("initial_state")
    @classmethod
    def validate_initial_state(
        cls, v: list[JsonDict], info: ValidationInfo
    ) -> list[JsonDict]:
        for state_dict in v:
            if state_dict.get("type") == EventTypes.JoinRules:
                if state_dict.get("content", {}).get("join_rule") != Membership.INVITE:
                    raise ValueError(
                        f"{info.field_name} contains join_rule that is not 'invite'"
                    )
            if state_dict.get("type") == EventTypes.GuestAccess:
                if (
                    state_dict.get("content", {}).get("guest_access")
                    != GuestAccess.FORBIDDEN
                ):
                    raise ValueError(
                        f"{info.field_name} contains guest_access that is not 'forbidden'"
                    )
        return v


class AssignGroupsToManagedRoomRequest(BaseModel):
    """Request body for assigning groups to a managed room."""

    groups: list[str]

    model_config = ConfigDict(extra="forbid")
