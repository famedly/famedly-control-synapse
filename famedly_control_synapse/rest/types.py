from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_core.core_schema import ValidationInfo
from synapse.api.constants import (
    CREATOR_POWER_LEVEL,
    EventTypes,
    GuestAccess,
    Membership,
)
from synapse.types import JsonDict
from typing_extensions import Self


class InvalidRequestError(ValueError):
    """Raised when the parameters for a request are invalid."""


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


PROTECTED_EVENT_TYPES = {
    EventTypes.PowerLevels: CREATOR_POWER_LEVEL - 1,
    EventTypes.JoinRules: CREATOR_POWER_LEVEL - 1,
    EventTypes.GuestAccess: CREATOR_POWER_LEVEL - 1,
}


class PowerLevelEventContent(BaseModel):
    """Power level event content for overriding default power levels."""

    # A full model validator is below to verify that membership action power levels can
    # not be overridden
    ban: int = CREATOR_POWER_LEVEL - 1
    # events has a validator below
    events: dict[str, int] = Field(
        default_factory=lambda: {
            EventTypes.PowerLevels: CREATOR_POWER_LEVEL - 1,
            EventTypes.JoinRules: CREATOR_POWER_LEVEL - 1,
            EventTypes.GuestAccess: CREATOR_POWER_LEVEL - 1,
        }
    )
    # Use the `exclude_if` pattern for ensuring that this field is not included if it is
    # the default per the matrix spec. This allows these defaults to not be included
    # when serializing this model. Other options included having `exclude_unset` and
    # `exclude_defaults` from `model_dump()`, but that would mean the fields we override
    # to also be excluded and that is unwanted.
    events_default: int = Field(0, exclude_if=lambda x: x == 0)
    invite: int = CREATOR_POWER_LEVEL - 1
    kick: int = CREATOR_POWER_LEVEL - 1
    redact: int = Field(50, exclude_if=lambda x: x == 50)
    state_default: int = Field(50, exclude_if=lambda x: x == 50)
    # users has a validator below
    users: dict[str, int] = Field(default_factory=dict)
    users_default: int = Field(0, exclude_if=lambda x: x == 0)
    notifications: dict[str, int] = Field(default_factory=dict)

    @field_validator("events")
    @classmethod
    def validate_events(cls, v: dict[str, int], info: ValidationInfo) -> dict[str, int]:
        """
        Ensure that certain event types can not have their power level overridden
        """
        for event_type, power_level in v.items():
            # First check for the specific event types that are not allowed to be
            # changed away from the room creator
            if event_type in PROTECTED_EVENT_TYPES:
                immutable_power_level = PROTECTED_EVENT_TYPES.get(event_type)
                if power_level != immutable_power_level:
                    # This will return a 400 on the room creation endpoint
                    msg = f"Changing power_level of '{event_type}' in {info.field_name} is forbidden!"
                    raise InvalidRequestError(msg)
        return v

    @field_validator("users")
    @classmethod
    def validate_users(cls, v: dict[str, int], info: ValidationInfo) -> dict[str, int]:
        """
        Ensure that any user that is not the room creator is not allowed to have the
        same power level as the room creator
        """
        # The context object is of type ContextT(and appears to be a proxy object type),
        # but only if it was passed into the model validation.
        if not isinstance(info.context, dict):
            msg = "Context should be passed into the model validation in the form of a dict"
            raise InvalidRequestError(msg)

        room_creator: str | None = info.context.get("room_creator")
        if not room_creator:
            msg = "Room creator key was found in context, but not a usable value"
            raise InvalidRequestError(msg)

        for user, power_level in v.items():
            if user != room_creator and power_level == CREATOR_POWER_LEVEL - 1:
                msg = "Can not have a user with that high a power level, only the room creator"
                raise InvalidRequestError(msg)
            if user == room_creator and power_level != CREATOR_POWER_LEVEL - 1:
                msg = "Can not change the room creator's power level"
                raise InvalidRequestError(msg)
        return v

    @model_validator(mode="before")
    @classmethod
    def merge_events_overrides(cls, data: Any) -> Any:
        """
        Allow merging new 'events' overrides into the expected data. This will not do
        validation
        """
        if isinstance(data, dict):
            # Retrieve the defaults from the default_factory on the 'events' object
            event_field: FieldInfo = cls.model_fields["events"]

            # mypy thinks that default_factory() here has both too few arguments and
            # that None is not callable. Neither of these is true
            default_events_items = event_field.default_factory()  # type: ignore[misc, call-arg]

            if "events" in data and isinstance(data["events"], dict):
                # Update the defaults with those passed in on creation of this object
                default_events_items.update(data["events"])
                # Overwrite those continuing on into the object creation
                data["events"] = default_events_items

            # if there was no 'events' being asked for, then the normal default_factory
            # will fill in the defaults for us
        return data

    @model_validator(mode="after")
    def validate_membership_actions(self) -> Self:
        """
        Ensure that the membership action power levels can not be overridden
        """
        for action in ("ban", "invite", "kick"):
            action_value = getattr(self, action)
            if action_value != CREATOR_POWER_LEVEL - 1:
                msg = f"Membership action('{action}') power level can not be overridden"
                raise InvalidRequestError(msg)
        return self


class CreateManagedRoomRequest(BaseModel):
    """Request body for creating a managed room.

    None of the fields are required. Any field that is omitted falls back to a
    default: ``room_alias_name`` and ``name`` are left unset, ``groups`` defaults
    to an empty list, and ``room_version`` is filled in with the server's
    default before validation (see ``CreateManagedRoomResource.on_POST``).
    """

    room_alias_name: str | None = None
    name: str | None = None
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
    room_version: str
    groups: list[str] = Field(default_factory=list)
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
            if state_dict.get("type") == EventTypes.JoinRules and (
                state_dict.get("content", {}).get("join_rule") != Membership.INVITE
            ):
                raise ValueError(
                    f"{info.field_name} contains join_rule that is not 'invite'"
                )
            if state_dict.get("type") == EventTypes.GuestAccess and (
                state_dict.get("content", {}).get("guest_access")
                != GuestAccess.FORBIDDEN
            ):
                raise ValueError(
                    f"{info.field_name} contains guest_access that is not 'forbidden'"
                )
            if state_dict.get("type") == EventTypes.PowerLevels:
                PowerLevelEventContent.model_validate(
                    state_dict.get("content"), context=info.context
                )
        return v


class AssignGroupsToManagedRoomRequest(BaseModel):
    """Request body for assigning groups to a managed room."""

    groups: list[str]

    model_config = ConfigDict(extra="forbid")
