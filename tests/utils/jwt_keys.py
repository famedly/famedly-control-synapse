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
"""Test fixtures for the JWT auth flow.

Generates one RSA keypair for the whole test session and writes both a JWK file
and a Zitadel-service-account-shaped file to a temp directory, so tests can point
``jwk_path`` / ``zitadel_service_account_path`` at real files on disk.
"""

import json
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

KID = "test-kid"
USER_ID = "service-user-123"

_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

PUBLIC_PEM = (
    _private_key.public_key()
    .public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    .decode("utf-8")
)


def _jwk_json() -> str:
    jwk = json.loads(RSAAlgorithm.to_jwk(_private_key))
    jwk["kid"] = KID
    jwk["alg"] = "RS256"
    return json.dumps(jwk)


def _service_account_json() -> str:
    pem = _private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode("utf-8")
    return json.dumps(
        {"type": "serviceaccount", "keyId": KID, "key": pem, "userId": USER_ID}
    )


_dir = Path(tempfile.mkdtemp(prefix="fc_jwt_test_"))
JWK_PATH = str(_dir / "jwk.json")
SERVICE_ACCOUNT_PATH = str(_dir / "service_account.json")
Path(JWK_PATH).write_text(_jwk_json(), encoding="utf-8")
Path(SERVICE_ACCOUNT_PATH).write_text(_service_account_json(), encoding="utf-8")

# A ready-made jwt_auth config block pointing at the generated JWK file, for reuse
# by tests that just need auth to construct successfully.
JWT_AUTH_CONFIG = {
    "token_endpoint": "http://dummy.test/oauth/token",
    "aud": "https://idp.example.com",
    "iss": USER_ID,
    "sub": USER_ID,
    "jwk_path": JWK_PATH,
}
