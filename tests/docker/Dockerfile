# Test image. Mirrors the pytest job in .github/workflows/ci.yml, so a
# green run here means a green gate.
#
# The dependency set is deliberately duplicated from the workflow rather
# than shared through a requirements file: the two-step pip install below
# is the point, and a requirements file cannot express "resolve with
# pytest 9, then downgrade". Change one, change the other.
FROM python:3.14-slim

# Home Assistant needs a compiler for a couple of wheels that have no
# musl/slim build, and git for packages that resolve to a VCS URL.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc git \
    && rm -rf /var/lib/apt/lists/*

# Tests that make a file unwritable and then assert on it pass silently
# as root. Run as somebody who can actually be denied.
RUN useradd --create-home --uid 1000 runner

USER runner
ENV PATH="/home/runner/.local/bin:${PATH}"

# Same two steps as the workflow, same reasons:
#
#   1) pytest-haX pins pytest==9.0.x in its metadata, but 9.x changed the
#      async-fixture handler in a way that breaks the plugin at runtime.
#      Resolve it with its full dep set first ...
#   2) ... then downgrade pytest, which uninstalls 9.x and installs 8.x.
#
# The homeassistant cap keeps pip on a stable release: pytest-haX pins one
# exact HA version and its newest release always tracks the next HA beta.
# voluptuous-serialize is imported directly by tests/test_config_flow.py.
RUN pip install --no-cache-dir --user \
        "homeassistant<2026.9" \
        "pytest-homeassistant-custom-component>=0.13.316" \
        pytest-mock \
        pytest-timeout \
        pytest-xdist \
        voluptuous-serialize \
        "pysunspec2[serial]==1.3.6" \
    && pip install --no-cache-dir --user "pytest<9"

WORKDIR /src

# The source is mounted read-only, so keep every write out of it.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTEST_ADDOPTS="-p no:cacheprovider --timeout=60"

CMD ["pytest", "tests/", "-q"]
