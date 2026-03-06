from typing import Iterable
from unittest.mock import AsyncMock, patch

from synapse.http.client import SimpleHttpClient
from synapse.http.site import SynapseRequest
from synapse.server import HomeServer
from synapse.types import JsonDict, UserID
from twisted.internet.testing import MemoryReactorClock
from twisted.web.server import Request, Site

from famedly_control_synapse.client import GroupMembersResponse, MemberInfo
from famedly_control_synapse.config import FamedlyControlConfig
from tests.utils.server import CustomHeaderType, FakeChannel, make_request


class FamedlyRestHelper:
    """
    A helper to use the proposed FamedlyControl endpoints on the Synapse module. This
    allows for patching in-place the responses that would be requested from the main
    FamedlyControl module endpoints.

    Some prep work may be needed while setting up your test HomeServer:
    * After you have registered your user(before or after login) call
      `register_external_id()` to add that user to the required tables and prepare it
      for having a group/group_id. If you use the supplied register_user() like normal,
      this will be handled for you.
    * Before creating a managed room, prepare your group by calling create_group(). This
      will look up the needed information to handle the external user id's and set the
      group up so it can be "retrieved" from /get_group_members on the remote service.
    """

    hs: HomeServer
    reactor: MemoryReactorClock
    site: Site
    config: FamedlyControlConfig
    groups_to_ids: dict[str, list[str]]
    """Retrieving a group_id should return a list of external user_ids"""

    mxids_to_external_ids: dict[str, str]
    """Mapping of mxids to external_ids, used to avoid lookups when creating groups"""

    BASE_PATH = "/_famedlyControl/v1/managedRooms"
    LIST_PATH = BASE_PATH + "/rooms"
    CREATE_PATH = BASE_PATH + "/createRoom"

    def __init__(
        self,
        hs: HomeServer,
        reactor: MemoryReactorClock,
        site: Site,
        config: FamedlyControlConfig,
    ) -> None:
        self.hs = hs
        self.reactor = reactor
        self.site = site
        self.config = config
        self.groups_to_ids = {}
        self.mxids_to_external_ids = {}

    def create_group(self, group_id: str, list_of_mxid_ids: list[str]) -> None:
        """
        Retrieve the associated external user ids to the supplied mxids. Add the
        external user ids to the group_id.

        To make this work automatically, make sure you have called
        register_external_id() with your desired mxid/external_id. This data will be
        retrieved for you to produce the mapping for responding to simulated calls to
        /get_group_members
        """
        list_of_external_ids = []
        for mxid in list_of_mxid_ids:
            assert mxid in self.mxids_to_external_ids, (
                f"The requested mxid('{mxid}') was not found to have an external user id. Did you "
                "call register_external_id()?"
            )
            list_of_external_ids.append(self.mxids_to_external_ids[mxid])
        self.groups_to_ids[group_id] = list_of_external_ids

    def remove_user_from_group(self, mxid: str, group_id: str) -> None:
        """
        Remove this mxid's associated external user id from the group
        """
        assert (
            group_id in self.groups_to_ids
        ), f"Group ('{group_id}') not found in group data. Did you create it with create_group()?"
        assert (
            mxid in self.mxids_to_external_ids
        ), f"Mxid('{mxid}') not found to have an external user id while coordinating removal from a group('{group_id}')"
        external_user_id = self.mxids_to_external_ids[mxid]
        self.groups_to_ids[group_id].remove(external_user_id)

    def add_user_to_group(self, mxid: str, group_id: str) -> None:
        """
        Add this mxid's associated external user id to a group
        """
        assert (
            group_id in self.groups_to_ids
        ), f"Group '{group_id}' not found in group data. Did you create it with create_group()?"
        assert (
            mxid in self.mxids_to_external_ids
        ), f"Mxid('{mxid}') not found to have an external user id while coordinating addition to a group('{group_id}')"
        external_user_id = self.mxids_to_external_ids[mxid]
        existing_group_list = self.groups_to_ids[group_id]
        assert (
            external_user_id not in existing_group_list
        ), f"Mxid('{mxid}') associated external user id was found in the requested group('{group_id}') already"
        self.groups_to_ids[group_id].append(external_user_id)

    async def register_external_id(
        self,
        local_mxid: str,
        requested_external_id: str | None = None,
        auth_provider_override: str | None = None,
    ) -> None:
        """
        Register an external user id into synapse.

        The auth_provider is set in the FamedlyControl configuration under
        'auth_provider'. For testing, it is generally configured to be
        "https://idp.example.com/" but overriding is allowed.

        When not provided, the requested_external_id is set to an email address format
        of the provided mxid. This is then saved to the mapping to assist in creating
        groups
        """
        if not requested_external_id:
            parsed_mxid = UserID.from_string(local_mxid)
            requested_external_id = f"{parsed_mxid.localpart}@{parsed_mxid.domain}"

        await self.hs.get_datastores().main.db_pool.simple_insert(
            table="user_external_ids",
            values={
                "user_id": local_mxid,
                "external_id": requested_external_id,
                "auth_provider": auth_provider_override or self.config.auth_provider,
            },
        )
        self.mxids_to_external_ids[local_mxid] = requested_external_id

    def make_request(
        self,
        method: bytes | str,
        path: bytes | str,
        content: bytes | str | JsonDict = b"",
        access_token: str | None = None,
        request: type[Request] = SynapseRequest,
        shorthand: bool = True,
        federation_auth_origin: bytes | None = None,
        content_is_form: bool = False,
        await_result: bool = True,
        custom_headers: Iterable[CustomHeaderType] | None = None,
        client_ip: str = "127.0.0.1",
    ) -> FakeChannel:
        """
        Create a SynapseRequest at the path using the method and containing the
        given content.

        Borrowed from the HomeserverTestCase class and added missing docstring args.

        Args:
            method: The HTTP request method ("verb").
            path: The HTTP path, suitably URL encoded (e.g. escaped UTF-8 & spaces
                and such). content (bytes or dict): The body of the request.
                JSON-encoded, if a dict.
            content: The JSON dict content passing into the request. Only useful for
                POST and PUT.
            access_token: The user access token to use for authorizing the request.
            request: Used for special types of Request class. Don't change unless you
                know what you are doing.
            shorthand: Whether to try and be helpful and prefix the given URL
            with the usual REST API path, if it doesn't contain it.
            federation_auth_origin: if set to not-None, we will add a fake
                Authorization header pretending to be the given server name.
            content_is_form: Whether the content is URL encoded form data. Adds the
                'Content-Type': 'application/x-www-form-urlencoded' header.

            await_result: whether to wait for the request to complete rendering. If
                 true (the default), will pump the test reactor until the the renderer
                 tells the channel the request is finished.

            custom_headers: (name, value) pairs to add as request headers

            client_ip: The IP to use as the requesting IP. Useful for testing
                ratelimiting.

        Returns:
            The FakeChannel object which stores the result of the request.
        """
        return make_request(
            self.reactor,
            self.site,
            method,
            path,
            content,
            access_token,
            request,
            shorthand,
            federation_auth_origin,
            content_is_form,
            await_result,
            custom_headers,
            client_ip,
        )

    def create_managed_room(self, content: JsonDict, access_token: str) -> FakeChannel:
        """
        Create a managed room using the local endpoint the module provides. Remember
        that for a room to be created with this endpoint, an external user id must have
        been assigned in the Synapse database, and a group_id must have been added
        containing the users
        """
        # From my understanding, groups is always a list even when empty
        extracted_groups = content.get("groups", [])
        response_list_of_group_member_responses = []

        # Create an iterable to give to AsyncMock as a side_effect. Each time the
        # patched function is called, the next iteration is returned
        for group_id in extracted_groups:
            list_of_member_info_objects = []
            for external_id in self.groups_to_ids.get(group_id, []):
                member_info = MemberInfo(user_id=external_id)
                list_of_member_info_objects.append(member_info)
            group_member_response = GroupMembersResponse(
                members=list_of_member_info_objects
            )
            # Make sure to use the kwarg 'by_alias=True' or the created key is not
            # right('external_user_id' and not 'user_id')
            response_list_of_group_member_responses.append(
                {"Ok": group_member_response.model_dump(by_alias=True)}
            )

        with patch.object(
            SimpleHttpClient,
            "post_json_get_json",
            AsyncMock(side_effect=response_list_of_group_member_responses),
        ):
            channel = self.make_request(
                method="POST",
                path=self.CREATE_PATH,
                content=content,
                access_token=access_token,
                shorthand=False,
            )
        return channel

    def assign_groups_to_managed_room(
        self, room_id: str, content: JsonDict, access_token: str
    ) -> FakeChannel:
        """
        Adjust membership in a room based on groups using the endpoint provided by this
        module. Just like for create_managed_room() above, an external user id must have
        been assigned in the Synapse database, and a group_id must have been added
        containing the users that are being assigned/adjusted/removed from the room
        """
        # From my understanding, groups is always a list even when empty
        extracted_groups = content.get("groups", [])
        response_list_of_group_member_responses = []

        # Create an iterable to give to AsyncMock as a side_effect. Each time the
        # patched function is called, the next iteration is returned
        for group_id in extracted_groups:
            list_of_member_info_objects = []
            for external_id in self.groups_to_ids.get(group_id, []):
                member_info = MemberInfo(user_id=external_id)
                list_of_member_info_objects.append(member_info)
            group_member_response = GroupMembersResponse(
                members=list_of_member_info_objects
            )
            # Make sure to use the kwarg 'by_alias=True' or the created key is not
            # right('external_user_id' and not 'user_id')
            response_list_of_group_member_responses.append(
                {"Ok": group_member_response.model_dump(by_alias=True)}
            )

        with patch.object(
            SimpleHttpClient,
            "post_json_get_json",
            AsyncMock(side_effect=response_list_of_group_member_responses),
        ):

            channel = self.make_request(
                method="POST",
                path=self.BASE_PATH + f"/{room_id}/groups",
                content=content,
                access_token=access_token,
                shorthand=False,
            )
        return channel
