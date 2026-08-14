# Copyright (C) 2026 Famedly
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
import json
import logging
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from synapse.module_api import ModuleApi
from synapse.util.async_helpers import ObservableDeferred
from twisted.internet import defer

from famedly_control_synapse.config import JwtAuthConfig

logger = logging.getLogger(__name__)

# Grant type for the OAuth2 JWT-bearer (private-key-jwt) token exchange.
_JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"

# Refresh a little before the cached token actually expires, so an in-flight
# request never travels with a token that expires en route.
_EXPIRY_BUFFER_SECONDS = 30


class JwtTokenProvider:
    """Obtains and caches an OAuth2 access token via the private-key-JWT flow.

    A short-lived JWT assertion is signed with the configured private key and
    exchanged at the IdP token endpoint for an access token. The token is cached
    and refreshed lazily, just before it expires or once it has expired.
    """

    def __init__(self, api: ModuleApi, cfg: JwtAuthConfig):
        self._http_client = api.http_client
        self._clock = api._hs.get_clock()
        self._cfg = cfg
        self._token_endpoint = cfg.token_endpoint.encoded_string()
        self._token: str | None = None
        self._expires_at: float = 0.0
        # How long before actual expiry a cached token is considered stale.
        # Recomputed per token in _fetch and clamped to the issued lifetime, so a
        # short-lived token isn't treated as immediately expired.
        self._refresh_buffer: float = 0.0
        # Shared in-flight fetch, so concurrent requests don't stampede the endpoint.
        # ObservableDeferred lets each caller observe the same fetch independently,
        # so a failed refresh is delivered to every waiter rather than only the first.
        self._refreshing: ObservableDeferred[None] | None = None

        # Load the signing key and its metadata once, from whichever source is set.
        self._key: Any
        if cfg.zitadel_service_account_path:
            account = json.loads(
                Path(cfg.zitadel_service_account_path).read_text(encoding="utf-8")
            )
            self._key = load_pem_private_key(
                account["key"].encode("utf-8"), password=None
            )
            self._kid = account["keyId"]
            self._alg = "RS256"
            # Zitadel ease-of-use: default iss/sub to the service user id.
            self._iss = cfg.iss or account["userId"]
            self._sub = cfg.sub or account["userId"]
        else:
            jwk = jwt.PyJWK.from_json(
                Path(cfg.jwk_path).read_text(encoding="utf-8")  # type: ignore[arg-type]
            )
            self._key = jwk.key
            self._kid = jwk.key_id
            self._alg = jwk.algorithm_name or "RS256"
            self._iss = cfg.iss
            self._sub = cfg.sub

    def _build_assertion(self) -> str:
        """Build and sign the JWT assertion presented to the token endpoint."""
        now = int(self._clock.time())
        claims = {
            "iss": self._iss,
            "sub": self._sub,
            "aud": self._cfg.aud,
            "iat": now,
            "exp": now + self._cfg.token_lifetime,
        }
        # PyJWT fills 'alg' from the key/algorithm but not 'kid', which Zitadel
        # requires, so it is set explicitly in the header.
        return jwt.encode(
            claims, self._key, algorithm=self._alg, headers={"kid": self._kid}
        )

    async def _fetch(self) -> None:
        scope = " ".join(dict.fromkeys(["openid", *self._cfg.scopes]))
        response = await self._http_client.post_urlencoded_get_json(
            self._token_endpoint,
            {
                "grant_type": _JWT_BEARER_GRANT,
                "scope": scope,
                "assertion": self._build_assertion(),
            },
        )
        try:
            token = response["access_token"]
            expires_in = response["expires_in"]
        except KeyError as e:
            raise ValueError(
                f"token endpoint response missing {e} field: {response}"
            ) from e
        # Clamp the refresh buffer to at most half the issued lifetime, so tokens
        # with a very short expires_in still get a usable cache window instead of
        # being refreshed on every request.
        self._token = token
        self._refresh_buffer = min(_EXPIRY_BUFFER_SECONDS, expires_in / 2)
        self._expires_at = self._clock.time() + expires_in

    def invalidate(self) -> None:
        """Drop the cached token so the next request performs a fresh exchange.

        Called when the resource server rejects the current token (HTTP 401),
        which can happen before the local expiry buffer elapses.
        """
        self._token = None
        self._expires_at = 0.0

    async def get_access_token(self) -> str:
        """Return a valid access token, fetching a fresh one if needed."""
        if (
            self._token is None
            or self._clock.time() >= self._expires_at - self._refresh_buffer
        ):
            if self._refreshing is None:
                self._refreshing = ObservableDeferred(
                    defer.ensureDeferred(self._fetch()), consumeErrors=True
                )
            observer = self._refreshing
            try:
                await observer.observe()
            finally:
                self._refreshing = None
        assert self._token is not None
        return self._token
