import logging
from typing import Final, Literal

from pydantic import BaseModel, Field
from synapse.api.errors import SynapseError
from synapse.module_api import ModuleApi

from famedly_control_synapse.config import FamedlyControlConfig

logger = logging.getLogger(__name__)


class Membership:
    """Action type for group membership changes."""

    ADD: Final = "Add"
    REM: Final = "Rem"


class DiffRecord(BaseModel):
    """One record from a diff for a group."""

    external_user_id: str = Field(alias="user_id")
    action: Literal["Add", "Rem"]  # Add: User was added, Rem: User was removed


class ManyGroupsDiffResponse(BaseModel):
    """Response containing multiple group diffs."""

    next_sync: str
    data: dict[str, list[DiffRecord]]  # Mapping from Group IDs to list of changes


class MemberInfo(BaseModel):
    """External User Id."""

    external_user_id: str = Field(alias="user_id")


class GroupMembersResponse(BaseModel):
    """Response containing list of group members."""

    members: list[MemberInfo]


class FamedlyControlClient:
    def __init__(self, api: ModuleApi, config: FamedlyControlConfig):
        self.access_token = config.famedly_control.access_token
        self.url = config.famedly_control.api_url.encoded_string().rstrip("/")
        self.http_client = api.http_client
        self.sync = 0

    async def get_group_members(self, group_id: str) -> list[str]:
        """Get the current members of a group.

        Args:
            group_id: The UUID of the group.

        Returns:
            List of external user IDs who are members of the group.

        Raises:
            FamedlyControlError: If the API returns an error or network failure occurs.
        """
        uri = self.url + "/get_group_members"
        body = {
            "group_id": group_id,
        }
        try:
            response = await self.http_client.post_json_get_json(
                uri,
                body,
                headers={"Authorization": f"Bearer {self.access_token}"},
            )
            if "Ok" in response:
                validated_response = GroupMembersResponse.model_validate(response["Ok"])
                return [
                    member.external_user_id for member in validated_response.members
                ]
            elif "Err" in response:
                error_type = response["Err"].get("type")
                logger.error("Famedly Control API Error: %s", error_type)
                raise FamedlyControlError(
                    500, f"Famedly Control API Error: {error_type}"
                )
            else:
                logger.error("Famedly Control API Error: %s", response)
                raise FamedlyControlError(
                    500, f"Unexpected response format: {response}"
                )
        except FamedlyControlError:
            raise
        except Exception as e:
            logger.error("Famedly Control API Error: %s", e)
            raise FamedlyControlError(500, f"Famedly Control API Error: {e}") from e

    async def get_all_groups_diffs(
        self, sync: str | None, timeout: int = 30
    ) -> ManyGroupsDiffResponse:
        """Get group membership diffs for all known groups. Long polling.

        Warning: This can return a lot of data!

        Args:
            sync: Monotonically increasing sync token from previous response.
            timeout: How long to wait in seconds if response would be empty.

        Returns:
            ManyGroupsDiffResponse containing next_sync token and mapping of group diffs.

        Raises:
            FamedlyControlError: If the API returns an error or network failure occurs.
        """
        uri = self.url + "/get_all_groups_diffs"
        body: dict = {
            "timeout": timeout,
        }
        if sync is not None:
            body["sync"] = sync
        try:
            response = await self.http_client.post_json_get_json(
                uri,
                body,
                headers={"Authorization": f"Bearer {self.access_token}"},
            )
            if "Ok" in response:
                return ManyGroupsDiffResponse.model_validate(response["Ok"])
            elif "Err" in response:
                error_type = response["Err"].get("type")
                logger.error("Famedly Control API Error: %s", error_type)
                raise FamedlyControlError(
                    500, f"Famedly Control API Error: {error_type}"
                )
            else:
                logger.error("Famedly Control API Error: %s", response)
                raise FamedlyControlError(
                    500, f"Unexpected response format: {response}"
                )
        except FamedlyControlError:
            raise
        except Exception as e:
            logger.error("Famedly Control API Error: %s", e)
            raise FamedlyControlError(500, f"Famedly Control API Error: {e}") from e


class FamedlyControlError(SynapseError):
    """Base exception for FamedlyControl API errors."""

    code = 500
    msg = "An error occurred with the Famedly Control API"

    def __init__(self, code: int | None = None, msg: str | None = None):
        self.msg = msg or self.__class__.msg
        self.code = code or self.__class__.code
        super().__init__(self.code, self.msg)
