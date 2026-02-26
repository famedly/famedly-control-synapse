import logging

from synapse.module_api import ModuleApi

from famedly_control_synapse.config import FamedlyControlConfig


class FamedlyControlClient:
    def __init__(self, api: ModuleApi, config: FamedlyControlConfig):
        self.api_key = config.api_key
        self.url = config.url
        self.http_client = api.http_client

    async def get_group_members(self, group_id: str) -> list[str]:
        uri = str(self.url) + f"/groups/{group_id}"
        try:
            response = await self.http_client.get_json(
                uri, headers={"Authorization": f"Bearer {self.api_key}"}
            )
        except Exception as e:
            # Handle exceptions such as network errors or invalid responses
            logging.exception(f"Error fetching group members: {e}")
        return response.get("members", [])

    # TODO: convert the zitadel user ids into the synapse id, uisng the token authenticator
