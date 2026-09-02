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

# Which Home Assistant to test against. "stable" is what the CI gate
# uses; build with --build-arg HA_CHANNEL=beta to reproduce what the
# weekly HA beta workflow sees.
ARG HA_CHANNEL=stable

# The versions are resolved from PyPI at build time by the same script
# the workflows call, so this image and the gate agree without a version
# written down in two places. Docker caches the layer, so a rebuild after
# a new HA release needs --no-cache to actually move.
COPY .github/scripts/resolve_ha_versions.py /tmp/resolve_ha_versions.py

# Same two steps as the workflow, same reasons:
#
#   1) pytest-haX pins pytest==9.0.x in its metadata, but 9.x changed the
#      async-fixture handler in a way that breaks the plugin at runtime.
#      Resolve it with its full dep set first ...
#   2) ... then downgrade pytest, which uninstalls 9.x and installs 8.x.
#
# voluptuous-serialize is imported directly by tests/test_config_flow.py.
# The last two are the runtime requirements of manifest.json:
# modbus-connection with its tmodbus backend, named twice because Home
# Assistant ignores a package's extras when it checks what is installed
# (home-assistant-libs/modbus-connection#182).
RUN python /tmp/resolve_ha_versions.py --channel "${HA_CHANNEL}" > /tmp/versions \
    && HA=$(sed -n 's/^ha=//p' /tmp/versions) \
    && PHCC=$(sed -n 's/^phcc=//p' /tmp/versions) \
    && echo "Testing against homeassistant ${HA} via pytest-haX ${PHCC}" \
    && pip install --no-cache-dir --user \
        "homeassistant==${HA}" \
        "pytest-homeassistant-custom-component==${PHCC}" \
        pytest-mock \
        pytest-timeout \
        pytest-xdist \
        voluptuous-serialize \
        "modbus-connection[tmodbus]>=4.10.0,<5" \
        "tmodbus>=0.6.1" \
    && pip install --no-cache-dir --user "pytest<9"

WORKDIR /src

# The source is mounted read-only, so keep every write out of it.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTEST_ADDOPTS="-p no:cacheprovider --timeout=60"

CMD ["pytest", "tests/", "-q"]
