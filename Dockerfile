FROM python:3.13-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b AS base

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory /workspace

ENV PATH="/opt/snagentic/bin:${PATH}" \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN python -m venv /opt/snagentic

WORKDIR /workspace
COPY pyproject.toml README.md ./
COPY requirements/constraints.txt ./requirements/constraints.txt
COPY src ./src
# Packaged into the wheel as snagentic/_assets (see pyproject force-include).
COPY .github/extensions/snagentic/*.mjs ./.github/extensions/snagentic/
COPY ui/package.json ui/package-lock.json ./ui/
COPY ui/src ./ui/src
COPY ui/recipes ./ui/recipes
COPY copilot-plugin ./copilot-plugin

FROM base AS development
COPY tests ./tests
RUN python -m pip install --no-cache-dir -c requirements/constraints.txt -e ".[dev,docs]"

ENTRYPOINT ["python", "-m", "snagentic"]

FROM base AS runtime
RUN python -m pip install --no-cache-dir -c requirements/constraints.txt . \
    && groupadd --system --gid 10001 snagentic \
    && useradd --system --uid 10001 --gid snagentic --home-dir /home/snagentic \
        --create-home snagentic \
    && chown snagentic:snagentic /workspace

USER snagentic

ENTRYPOINT ["python", "-m", "snagentic"]
