import logging
from http import HTTPStatus
from typing import Final, Literal, TypeVar

from pydantic import BaseModel, Field
from synapse.api.errors import HttpResponseException
from synapse.module_api import ModuleApi
from synapse.module_api.errors import Codes, SynapseError
from synapse.types import JsonDict

from famedly_control_synapse.auth import JwtTokenProvider
from famedly_control_synapse.config import FamedlyControlConfig

logger = logging.getLogger(__name__)

_T = TypeVar("_T", bound=BaseModel)


class MembershipAction:
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


_ERROR_TYPE_TO_STATUS_CODE: dict[str, HTTPStatus] = {
    # This is based on GroupMembershipDiffApi.yaml
    "Internal": HTTPStatus.INTERNAL_SERVER_ERROR,
    "Forbidden": HTTPStatus.FORBIDDEN,
    "Unauthorized": HTTPStatus.UNAUTHORIZED,
    "Api": HTTPStatus.BAD_GATEWAY,
}


class FamedlyControlClient:
    def __init__(self, api: ModuleApi, config: FamedlyControlConfig):
        self._auth = JwtTokenProvider(api, config.famedly_control.jwt_auth)
        self.url = config.famedly_control.api_url.encoded_string().rstrip("/")
        self.http_client = api.http_client
        self.sync = 0

    async def _request(self, uri: str, body: dict, model: type[_T]) -> _T:
        """POST to the Famedly Control API and return a validated response model.

        Args:
            uri: The full URI to POST to.
            body: The JSON body to include in the POST request.
            model: The Pydantic model class to validate the "Ok" response against.

        Raises:
            FamedlyControlError: For all error conditions (API errors, network
                failures, validation errors, or any other unexpected exception).
        """
        try:
            token = await self._auth.get_access_token()
            response = await self.http_client.post_json_get_json(
                uri,
                body,
                headers={"Authorization": [f"Bearer {token}"]},
            )
            if "Ok" in response:
                return model.model_validate(response["Ok"])
            if "Err" in response:
                error_type = response["Err"].get("type")
                status_code = _ERROR_TYPE_TO_STATUS_CODE.get(
                    error_type, HTTPStatus.INTERNAL_SERVER_ERROR
                )
                if status_code == HTTPStatus.UNAUTHORIZED:
                    # The token was rejected; drop it so the next request exchanges
                    # a fresh one instead of resending the same rejected credential.
                    self._auth.invalidate()
                msg = f"Famedly Control API: Error in response: {error_type}"
                logger.error(msg)
                raise FamedlyControlError(status_code, msg)
            msg = f"Famedly Control API: Unexpected response format: {response}"
            logger.error(msg)
            raise FamedlyControlError(HTTPStatus.BAD_GATEWAY, msg)
        except FamedlyControlError:
            raise
        except HttpResponseException as e:
            if e.code == HTTPStatus.UNAUTHORIZED:
                # The token was rejected; drop it so the next request exchanges
                # a fresh one instead of resending the same rejected credential.
                self._auth.invalidate()
            msg = f"Famedly Control API: HTTP response error: {e.msg}"
            logger.error(msg)
            raise FamedlyControlError(e.code, msg) from e
        except Exception as e:
            msg = f"Famedly Control API: Unexpected error: {e}"
            logger.error(msg)
            raise FamedlyControlError(msg=msg) from e

    async def get_group_members(self, group_id: str) -> list[str]:
        """Get the current members of a group.

        Args:
            group_id: The UUID of the group.

        Returns:
            List of external user IDs who are members of the group.

        Raises:
            FamedlyControlError: If the API returns an error or network failure occurs.
        """
        response = await self._request(
            self.url + "/get_group_members",
            {"group_id": group_id},
            GroupMembersResponse,
        )
        return [member.external_user_id for member in response.members]

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
        body: dict = {"timeout": timeout}
        if sync is not None:
            body["sync"] = sync
        return await self._request(
            self.url + "/get_all_groups_diffs",
            body,
            ManyGroupsDiffResponse,
        )


class FamedlyControlError(SynapseError):
    """Base exception for FamedlyControl API errors."""

    def __init__(
        self,
        code: int | None = None,
        msg: str | None = None,
        errcode: str | None = None,
        additional_fields: JsonDict | None = None,
    ):
        super().__init__(
            code or HTTPStatus.INTERNAL_SERVER_ERROR,
            msg or "An error occurred with the Famedly Control API",
            errcode=errcode or Codes.UNKNOWN,
            additional_fields=additional_fields,
        )
