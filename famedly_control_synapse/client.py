import logging
from typing import Final, Literal

from pydantic import BaseModel
from synapse.module_api import ModuleApi

from famedly_control_synapse.config import FamedlyControlConfig

logger = logging.getLogger(__name__)


class Membership:
    """Action type for group membership changes."""

    ADD: Final = "Add"
    REM: Final = "Rem"


class DiffRecord(BaseModel):
    """One record from a diff for a group."""

    user_id: str  # External user ID
    action: Literal["Add", "Rem"]  # Add: User was added, Rem: User was removed


class GroupDiffResponse(BaseModel):
    """Response containing group diff data."""

    next_sync: str  # Current sync token to pass in next request
    data: list[DiffRecord]  # List of changes for one group


class ManyGroupsDiffResponse(BaseModel):
    """Response containing multiple group diffs."""

    next_sync: str
    data: dict[str, list[DiffRecord]]  # Mapping from Group IDs to list of changes


class FamedlyControlClient:
    def __init__(self, api: ModuleApi, config: FamedlyControlConfig):
        self.api_key = config.api_key
        self.url = config.url
        self.http_client = api.http_client
        self.sync = 0

    async def get_group_members(self, group_id: str) -> list[str]:
        # WIP: There will be proper get_group_members endpoint provided by the external API.
        # And the changes will be handled later pr.
        """Get the current members of a group.

        Args:
            group_id: The UUID of the group.

        Returns:
            List of external user IDs who are members of the group.

        Raises:
            Exception: If the API returns an error or network failure occurs.
        """
        group_diff = await self.get_group_diff(group_id, sync="0", timeout=0)
        return [
            record.user_id
            for record in group_diff.data
            if record.action == Membership.ADD
        ]

    async def get_group_diff(
        self, group_id: str, sync: str, timeout: int = 30
    ) -> GroupDiffResponse:
        """Get group membership diff for one particular group. Long polling.

        Args:
            group_id: The UUID of the group.
            sync: Monotonically increasing sync token from previous response.
            timeout: How long to wait in seconds if response would be empty.

        Returns:
            GroupDiffResponse containing next_sync token and list of diff records.

        Raises:
            Exception: If the API returns an error or network failure occurs.
        """
        uri = str(self.url) + "/get_group_diff"
        body = {
            "group_id": group_id,
            "sync": sync,
            "timeout": timeout,
        }

        try:
            response = await self.http_client.post_json_get_json(
                uri,
                body,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            if "Ok" in response:
                return GroupDiffResponse.model_validate(response["Ok"])
            else:
                raise Exception(f"Unexpected response: {response}")

        except Exception as e:
            logger.exception("Error fetching group diff: %s", e)
            raise

    async def get_all_groups_diffs(
        self, sync: str, timeout: int = 30
    ) -> ManyGroupsDiffResponse:
        """Get group membership diffs for all known groups. Long polling.

        Warning: This can return a lot of data!

        Args:
            sync: Monotonically increasing sync token from previous response.
            timeout: How long to wait in seconds if response would be empty.

        Returns:
            ManyGroupsDiffResponse containing next_sync token and mapping of group diffs.

        Raises:
            Exception: If the API returns an error or network failure occurs.
        """
        uri = str(self.url) + "/get_all_groups_diffs"
        body = {
            "sync": sync,
            "timeout": timeout,
        }

        try:
            response = await self.http_client.post_json_get_json(
                uri,
                body,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            if "Ok" in response:
                return ManyGroupsDiffResponse.model_validate(response["Ok"])
            else:
                raise Exception(f"Unexpected response: {response}")

        except Exception as e:
            logger.exception("Error fetching all groups diffs: %s", e)
            raise
