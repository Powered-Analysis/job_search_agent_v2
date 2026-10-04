FROM python:3.14-slim-trixie

# Python reads timezone rules from the system, and the slim image ships none.
RUN apt-get update \
    && apt-get install --no-install-recommends --yes tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

# The Claude Code CLI refuses to skip permission prompts as root.
RUN useradd --create-home --uid 1000 jsa
WORKDIR /app

# Dependencies first, so a code-only change reuses the cached layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# Only profile/search/ survives .dockerignore (XC-11).
COPY profile ./profile

ENV PATH="/app/.venv/bin:$PATH"
USER jsa
ENTRYPOINT ["jsa", "cron"]
