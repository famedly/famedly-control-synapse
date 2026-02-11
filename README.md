# Famedly Control Synapse

[![PyPI - Version](https://img.shields.io/pypi/v/famedly-control-synapse.svg)](https://pypi.org/project/famedly-control-synapse)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/famedly-control-synapse.svg)](https://pypi.org/project/famedly-control-synapse)

---

**Table of Contents**

- [Installation](#installation)
- [Configuration](#configuration)
- [Testing](#testing)
- [Code Quality](#code-quality)
- [License](#license)

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
        title: "Famedly Control API by Famedly", # Title for the info endpoint, optional
        description: "Custom description for the endpoint", # Description for the info endpoint, optional
        contact: "random@example.com", # Contact information for the info endpoint, optional
        url: str = "", # Prefix of the current famedly control http API
        access_token: str = "", # Access token to authenticate against famedly control
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
