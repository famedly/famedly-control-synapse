# Famedly Control Synapse

[![PyPI - Version](https://img.shields.io/pypi/v/famedly-control-synapse.svg)](https://pypi.org/project/famedly-control-synapse)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/famedly-control-synapse.svg)](https://pypi.org/project/famedly-control-synapse)

---

**Table of Contents**

- [Documentation](#documentation)
- [Installation](#installation)
- [Configuration](#configuration)
- [Testing](#testing)
- [Code Quality](#code-quality)
- [License](#license)

## Documentation

This repository contains a [Synapse](https://github.com/famedly/synapse) module which allows for creating and managing "managed rooms". Those rooms have the following properties: invite-only, non-federated, cannot be left. Users are associated to `groups` in an external service and those same `groups` are associated to rooms in this module. Users are then automatically added to the corresponding rooms. This module is only "half of the work", the other half being in the external service.

The openapi specification is available at [openapi-spec.yaml](openapi-spec.yaml). You can use tools like [swagger.io](https://editor.swagger.io/) for a more readable format.

Note for users being deactivated: Highly recommended to remove a user from a group before it is deactivated. Upon deactivation, the user will be removed from the room regardless of any group. If the user should be reactivated in the future, it will not automatically re-join any rooms its groups are in.

### Worker routing

The HTTP endpoints exposed by this module are only registered on the background task synapse worker (or the main process if no dedicated worker is configured). Requests to these endpoints on any other worker will return `404 NOT_FOUND`.

This must be taken into account when configuring a reverse proxy for example; all requests under `/_famedlyControl/` must be routed to the background task worker.

## Installation

```console
pip install famedly-control-synapse
```

## Configuration

Here are the available configuration options:

```yaml
# the outer modules section is just provided for completeness, the config block is the actual module config.
modules:
  - module: "famedly_control_synapse.FamedlyControl"
    config:
        famedly_control:
          api_url: str = "", # Prefix of the current famedly control http API
          jwt_auth: # OAuth2 private-key-JWT authentication against the IdP (see "Authentication" below)
            token_endpoint: str = "" # Token exchange endpoint, e.g. https://CUSTOM_DOMAIN/oauth/v2/token
            aud: str = "" # Audience of the JWT assertion; the domain of the Zitadel instance
            iss: str | null = null # Service user id (see below)
            sub: str | null = null # Service user id (see below)
            scopes: list[str] = [] # Extra scopes for the token exchange; "openid" is always added
            token_lifetime: int = 3600 # Lifetime in seconds of the signed JWT assertion
            jwk_path: str | null = null # Signing key source (see below)
            zitadel_service_account_path: str | null = null # Signing key source (see below)
        sync_enabled: bool = true, # Whether to run the background group membership sync loop
        sync_polling_interval_seconds: int = 30, # Interval in seconds between polling requests
        error_retry_queue_enabled: bool = true, # Enable retrying membership changes that previously errored
        error_retry_queue_interval_seconds: int = 30, # Interval in seconds between retrying a failed membership change
        error_retry_queue_log_after_retry_count: int = 3, # The count of error retry queue attempts to start logging warnings
        auth_provider: str = "", # The unique, internal ID of the external identity provider.
        admin_user: str = "", # The localpart of the sole administrator allowed to call the API
```

### Authentication (`jwt_auth`)

The module authenticates to Famedly Control using the OAuth2
[private-key-JWT](https://zitadel.com/docs/guides/integrate/service-accounts/private-key-jwt)
flow rather than a static token. It signs a short-lived JWT assertion with a private key,
exchanges it at `token_endpoint` for an access token, and uses that as the `Bearer`
credential for API requests. The access token is refreshed automatically before it
expires.

The signing key can be provided in one of two ways:

- **`zitadel_service_account_path`**: path to a Zitadel service account JSON (contains the
  RSA private key, its `keyId`, and the service `userId`). `iss` and `sub` default to the file's `userId` when not set
  explicitly. Internally the key is converted to a JWK.
- **`jwk_path`**: path to a JSON Web Key holding the private key, with `kid` set (and `alg`
  optionally set/inferred). When using a raw JWK, `iss` and `sub` are required.
### API access restriction

The Famedly Control API is restricted to a single Matrix user, configured via the
`admin_user` option. Every API request from any other user (including other server
admins) is rejected with `403 M_FORBIDDEN`.

This restriction exists because the module stores its management information in the
`admin_user`'s account data. Allowing multiple admins would risk splitting that
management state across users. The configured `admin_user` should be a server admin.

## Testing

To create a virtual development environment and install dependencies:
```console
hatch shell
```

The tests use pytest, with the development environment managed by Hatch. Running the tests
can be done like this:

```console
hatch test
```

#### Additional optional testing arguments:
Run the tests in parallel: `-p`

Collect coverage data (automatically output as `lcov.info`): `-c`

#### Running a specific test:
Selecting a specific test to run can be as easy as providing the path to the test. All tests start from
the base test directory, `tests`. If running all tests, this can be left out. For specific tests, see
the [pytest usage docs](https://docs.pytest.org/en/stable/how-to/usage.html#specifying-which-tests-to-run) for more information.

## Code Quality

Use `hatch fmt` to automatically format code, enforce style rules, and check types using:

- `black` and `isort` for formatting
- `ruff` for linting
- `mypy` for static type checking

### Check Code Without Modifying It

To check code quality without modifying files:

- Check formatting with `isort` and `black`:
  ```console
  hatch fmt --check -f
  ```
- Check types and linting with `mypy` and `ruff`:
  ```console
  hatch fmt --check -l
  ```
- Check all of above, formatting, linting, and typing:
  ```console
  hatch fmt --check
  ```

### Auto-formatting Code

To automatically fix issues in the code:

- Format only using `black` and `isort`:
  ```console
  hatch fmt -f
  ```
- Type checks(`mypy`) and lint, fixing autofixable `ruff` issues:
  ```console
  hatch fmt -l
  ```
- Run all tools, format, lint, type-check:
  ```console
  hatch fmt
  ```

## License

`famedly-control-synapse` is distributed under the terms of the
[AGPL-3.0](https://spdx.org/licenses/AGPL-3.0-only.html) license.
