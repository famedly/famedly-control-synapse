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
from unittest.mock import AsyncMock, MagicMock

import jwt
from twisted.internet.defer import Deferred, ensureDeferred
from twisted.trial import unittest

from famedly_control_synapse.auth import JwtTokenProvider
from famedly_control_synapse.config import JwtAuthConfig
from tests.utils.jwt_keys import (
    JWK_PATH,
    PUBLIC_PEM,
    SERVICE_ACCOUNT_PATH,
    USER_ID,
)

AUD = "https://idp.example.com"


def _make_provider(*, exchange_response=None, **cfg_overrides):
    cfg_kwargs = {
        "token_endpoint": "http://dummy.test/oauth/token",
        "aud": AUD,
        "iss": "service-user",
        "sub": "service-user",
        "jwk_path": JWK_PATH,
    }
    cfg_kwargs.update(cfg_overrides)
    cfg = JwtAuthConfig(**cfg_kwargs)

    api = MagicMock()
    api.http_client = MagicMock()
    api.http_client.post_urlencoded_get_json = AsyncMock(
        return_value=exchange_response
        or {"access_token": "access-abc", "expires_in": 3600}
    )
    return api, JwtTokenProvider(api, cfg)


class TestJwtTokenProvider(unittest.TestCase):
    def test_assertion_headers_and_claims_and_signature(self):
        """The signed assertion carries kid/alg in the header and the required
        claims, and its signature verifies against the public key."""
        _, provider = _make_provider()
        assertion = provider._build_assertion()

        header = jwt.get_unverified_header(assertion)
        assert header["kid"] == "test-kid"
        assert header["alg"] == "RS256"

        # Decoding with the public key verifies the RSA signature; passing the
        # audience verifies the 'aud' claim.
        claims = jwt.decode(assertion, PUBLIC_PEM, algorithms=["RS256"], audience=AUD)
        assert claims["iss"] == "service-user"
        assert claims["sub"] == "service-user"
        assert claims["aud"] == AUD
        assert "iat" in claims
        assert claims["exp"] > claims["iat"]

    def test_bad_signature_rejected(self):
        """Sanity check that signature verification is actually meaningful."""
        _, provider = _make_provider()
        assertion = provider._build_assertion()
        tampered = assertion[:-4] + ("AAAA" if assertion[-4:] != "AAAA" else "BBBB")
        with self.assertRaises(jwt.InvalidSignatureError):
            jwt.decode(tampered, PUBLIC_PEM, algorithms=["RS256"], audience=AUD)

    def test_get_access_token_exchanges_assertion(self):
        api, provider = _make_provider()
        token = self.successResultOf(ensureDeferred(provider.get_access_token()))
        assert token == "access-abc"

        args = api.http_client.post_urlencoded_get_json.call_args
        assert args[0][0] == "http://dummy.test/oauth/token"
        body = args[0][1]
        assert body["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
        assert body["scope"] == "openid"
        # The assertion carried in the exchange is the signed JWT.
        jwt.decode(body["assertion"], PUBLIC_PEM, algorithms=["RS256"], audience=AUD)

    def test_extra_scopes_included(self):
        api, provider = _make_provider(scopes=["profile", "email"])
        self.successResultOf(ensureDeferred(provider.get_access_token()))
        body = api.http_client.post_urlencoded_get_json.call_args[0][1]
        assert body["scope"] == "openid profile email"

    def test_token_cached_until_expiry(self):
        api, provider = _make_provider()
        self.successResultOf(ensureDeferred(provider.get_access_token()))
        self.successResultOf(ensureDeferred(provider.get_access_token()))
        # A still-valid cached token must not trigger a second exchange.
        assert api.http_client.post_urlencoded_get_json.call_count == 1

    def test_token_refreshed_when_expired(self):
        api, provider = _make_provider()
        self.successResultOf(ensureDeferred(provider.get_access_token()))
        # Force the cached token to look expired.
        provider._expires_at = 0.0
        self.successResultOf(ensureDeferred(provider.get_access_token()))
        assert api.http_client.post_urlencoded_get_json.call_count == 2

    def test_concurrent_refresh_shares_single_exchange(self):
        """Callers arriving while a refresh is in flight share it, so the token
        endpoint is hit once and every caller gets the token."""
        api, provider = _make_provider()
        # Make the exchange pend until we fire it, so a second caller can arrive
        # while the first is still in flight.
        exchange: Deferred = Deferred()
        api.http_client.post_urlencoded_get_json = MagicMock(return_value=exchange)

        first = ensureDeferred(provider.get_access_token())
        second = ensureDeferred(provider.get_access_token())
        exchange.callback({"access_token": "access-abc", "expires_in": 3600})

        assert self.successResultOf(first) == "access-abc"
        assert self.successResultOf(second) == "access-abc"
        assert api.http_client.post_urlencoded_get_json.call_count == 1

    def test_concurrent_refresh_failure_reaches_all_callers(self):
        """A failed in-flight refresh must be delivered to every waiter, not just
        the first, otherwise later callers would return a stale/None token."""
        api, provider = _make_provider()
        exchange: Deferred = Deferred()
        api.http_client.post_urlencoded_get_json = MagicMock(return_value=exchange)

        first = ensureDeferred(provider.get_access_token())
        second = ensureDeferred(provider.get_access_token())
        exchange.errback(RuntimeError("token endpoint down"))

        self.failureResultOf(first, RuntimeError)
        self.failureResultOf(second, RuntimeError)

    def test_service_account_derives_iss_sub(self):
        _, provider = _make_provider(
            iss=None,
            sub=None,
            jwk_path=None,
            zitadel_service_account_path=SERVICE_ACCOUNT_PATH,
        )
        claims = jwt.decode(
            provider._build_assertion(), PUBLIC_PEM, algorithms=["RS256"], audience=AUD
        )
        assert claims["iss"] == USER_ID
        assert claims["sub"] == USER_ID

    def test_config_iss_sub_override_service_account(self):
        _, provider = _make_provider(
            iss="explicit-iss",
            sub="explicit-sub",
            jwk_path=None,
            zitadel_service_account_path=SERVICE_ACCOUNT_PATH,
        )
        claims = jwt.decode(
            provider._build_assertion(), PUBLIC_PEM, algorithms=["RS256"], audience=AUD
        )
        assert claims["iss"] == "explicit-iss"
        assert claims["sub"] == "explicit-sub"
