import logging
from collections.abc import Callable
from enum import Enum
from http import HTTPStatus
from typing import Final, Literal, NoReturn, TypeVar

from pydantic import BaseModel, Field, ValidationError
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


class FamedlyControlApiErrorCodes(str, Enum):
    UNKNOWN_SYNC_TOKEN = "UnknownSyncToken"


_ERROR_TYPE_TO_STATUS_CODE: dict[str, HTTPStatus] = {
    # This is based on GroupMembershipDiffApi.yaml
    "Internal": HTTPStatus.INTERNAL_SERVER_ERROR,
    "InvalidRequest": HTTPStatus.BAD_REQUEST,
    "Forbidden": HTTPStatus.FORBIDDEN,
    "Unauthorized": HTTPStatus.UNAUTHORIZED,
    "Api": HTTPStatus.BAD_GATEWAY,
}


class FamedlyControlErrorResponse(BaseModel):
    """An error response from Famedly Control API. Everything inside the `Err` object."""

    type: str
    errors: list[dict[str, str]] | None = None
    """Used by the 'InvalidRequest' error type. When present, should be an mapping of 'path' to a str and 'error' to a str."""

    def get_error_message(self) -> str | None:
        if self.type == "InvalidRequest":
            # "InvalidRequest" can have a few extra details that can be surfaced.
            return f"Famedly Control API: {self.type}, {self.errors=}"
        return f"Famedly Control API: Error in response: {self.type}"

    # TODO: after minimum python version becomes 3.11, change return type here to `Never` per python docs.
    def raise_famedly_control_error(
        self, auth_invalidate_cb: Callable[[], None]
    ) -> NoReturn:
        """
        This is an error class. Raise the error with appropriate messages and codes depending on what type of error it
        is.
        """
        if self.type not in _ERROR_TYPE_TO_STATUS_CODE:
            status_code = HTTPStatus.INTERNAL_SERVER_ERROR
            msg = f"Famedly Control API: Unknown error type: {self.type}"
            raise FamedlyControlError(status_code, msg)

        status_code = _ERROR_TYPE_TO_STATUS_CODE[self.type]

        if self.type == "Unauthorized":
            # The token was rejected; drop it so the next request exchanges a fresh one instead of resending the
            # same rejected credential.
            # XXX: This is not tested for!! I had forgotten to include it and tests passed
            auth_invalidate_cb()

        raise FamedlyControlError(status_code, self.get_error_message())


class FamedlyControlGroupDiffErrorResponse(FamedlyControlErrorResponse):
    """Special casing to surface Api specific errors such as for the UnknownSyncToken"""

    error: str | None = None
    """Used by the 'Api' error type"""

    def get_error_message(self) -> str | None:
        if self.type == "Api":
            # "Api" can have explicit sub-errors, but only when calling get_all_groups_diffs endpoint.
            if self.error == FamedlyControlApiErrorCodes.UNKNOWN_SYNC_TOKEN:
                raise FamedlyUnknownSyncTokenError()
            return f"Famedly Control API: {self.type}, {self.error=}"

        return super().get_error_message()


class FamedlyControlClient:
    def __init__(self, api: ModuleApi, config: FamedlyControlConfig):
        self._auth = JwtTokenProvider(api, config.famedly_control.jwt_auth)
        self.url = config.famedly_control.api_url.encoded_string().rstrip("/")
        self.http_client = api.http_client

    async def _request(
        self,
        uri: str,
        body: dict,
        model: type[_T],
        error_response_model: type[
            FamedlyControlErrorResponse
        ] = FamedlyControlErrorResponse,
    ) -> _T:
        """POST to the Famedly Control API and return a validated response model.

        Args:
            uri: The full URI to POST to.
            body: The JSON body to include in the POST request.
            model: The Pydantic model class to validate the "Ok" response against.
            error_response_model: The Pydantic model to use for validating the InfallibleApiError response

        Raises:
            FamedlyUnknownSyncTokenError: For specific error involving the requested 'sync' token. Triggers a sync loop
                reset.
            FamedlyControlError: For all error conditions (API errors, network
                failures, validation errors, or any other unexpected exception).
        """
        response = await self._post_json(uri, body)
        if "Err" in response:
            self._handle_err(response, error_response_model=error_response_model)
        try:
            return model.model_validate(response["Ok"])
        except (ValidationError, KeyError) as e:
            # KeyError from "Ok" not being in the `response` dict
            msg = f"Famedly Control API: Unexpected response format: {response}"
            logger.error(msg + f"\n{e}")
            raise FamedlyControlError(HTTPStatus.BAD_GATEWAY, msg)

    async def _post_json(self, uri: str, body: dict) -> JsonDict:
        try:
            token = await self._auth.get_access_token()
            return await self.http_client.post_json_get_json(
                uri,
                body,
                headers={"Authorization": [f"Bearer {token}"]},
            )
        except HttpResponseException as e:
            if e.code == HTTPStatus.UNAUTHORIZED:
                # The token was rejected; drop it so the next request exchanges
                # a fresh one instead of resending the same rejected credential.
                self._auth.invalidate()
            msg = f"Famedly Control API: HTTP response error: {e.msg}"
            logger.error(msg)
            raise FamedlyControlError(e.code, msg) from e
        except Exception as e:
            # Realistically, the known options here are RequestTimedOutError and ValueError. The second happens when it
            # is not parsable JSON. The first is a timeout on receiving the headers, not the body of the response.
            # XXX(jason): Is the timeout worth responding with a 504(Gateway Timeout) code?
            # This gets `None` as it will default to "M_UNKNOWN"
            errcode = Codes.NOT_JSON if isinstance(e, ValueError) else None
            msg = f"Famedly Control API: Unexpected error: {e}"
            logger.error(msg)
            raise FamedlyControlError(msg=msg, errcode=errcode) from e

    def _handle_err(
        self,
        response: JsonDict,
        error_response_model: type[FamedlyControlErrorResponse],
    ) -> NoReturn:
        try:
            err_object = response["Err"]
            err_response_model = error_response_model.model_validate(err_object)
        except ValidationError as e:
            msg = f"Famedly Control API: Unexpected error response format: {response}"
            logger.error(msg + f"\n{e}")
            raise FamedlyControlError(HTTPStatus.BAD_GATEWAY, msg)
        else:
            # This will raise directly
            err_response_model.raise_famedly_control_error(self._auth.invalidate)

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
            error_response_model=FamedlyControlGroupDiffErrorResponse,
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


class FamedlyUnknownSyncTokenError(Exception):
    pass
