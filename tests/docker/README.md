# Running the tests

The test suite does not run on Windows Python, because Home Assistant
imports `fcntl`. It runs here instead, in a container that installs what
the CI job installs.

## The full suite

```bash
docker compose -f tests/docker/compose.yml run --rm tests
```

First run builds the image, which takes a few minutes. After that a full
run is about three and a half minutes.

## One file, one test

Everything after the service name is passed to pytest:

```bash
docker compose -f tests/docker/compose.yml run --rm tests pytest tests/test_init.py -q
```

```bash
docker compose -f tests/docker/compose.yml run --rm tests pytest tests/test_init.py::test_setup_unload_and_reload_entry -x
```

## When a run is slow or a test hangs

`tests-fast` runs the suite across all cores. The output interleaves, so
use it when you expect green:

```bash
docker compose -f tests/docker/compose.yml run --rm tests-fast
```

`tests-verbose` streams each test name and log output as it goes, which
is what you want when something hangs and you need to know where:

```bash
docker compose -f tests/docker/compose.yml run --rm tests-verbose
```

Every test has a 60 second timeout. A hanging test fails and names
itself rather than taking the run down with it.

## After changing dependencies

The image installs them at build time, so rebuild:

```bash
docker compose -f tests/docker/compose.yml build tests
```

The dependency set lives in the `Dockerfile` and mirrors the pytest job
in `.github/workflows/ci.yml`. Change one, change the other. The comment
at the install step says why it is two steps and why Home Assistant is
capped.

## Why the source is read-only

It is mounted `:ro` with a tmpfs over `/tmp`, and the container runs as
an unprivileged user. A test that writes next to the source, or that
makes a file unwritable and then asserts it cannot be written, fails
here the same way it fails in CI. As root, and with a writable mount,
both of those pass locally and then break the gate.

## Lint and type checking

Those are not in the image. They run on the host and need no Home
Assistant:

```bash
uv run --with ruff ruff check custom_components/ tests/
```

```bash
uv run --with ruff ruff format --check custom_components/ tests/
```

mypy does need the full dependency set. The invocation is in `CLAUDE.md`
under "Type checking".
