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
          access_token: str = "", # Access token to authenticate against famedly control
        sync_enabled: bool = true, # Whether to run the background group membership sync loop
        sync_polling_interval_seconds: int = 30, # Interval in seconds between polling requests
        auth_provider: str = "", # The unique, internal ID of the external identity provider.
```

## Testing

To create virtual env and install dependency:
```console
hatch shell
```

The tests use `pytest`, with the development environment managed by Hatch. You can
run the test suite and generate a coverage report with:

```console
hatch run cov
```

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
