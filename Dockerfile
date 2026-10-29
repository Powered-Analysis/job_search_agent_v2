FROM python:3.14-slim-trixie

# Python reads timezone rules from the system, and the slim image ships none.
RUN apt-get update \
    && apt-get install --no-install-recommends --yes tzdata \
    && rm -rf /var/lib/apt/lists/*

# The inbox's packet build and tracker append shell out to `gws` (PRD 06). A pinned release.
# curl only fetches it, so it is purged afterwards; `gws` keeps ca-certificates for HTTPS.
ARG GWS_VERSION=0.22.5
RUN apt-get update \
    && apt-get install --no-install-recommends --yes ca-certificates curl \
    && arch="$(uname -m)" \
    && cd /tmp \
    && gws="google-workspace-cli-${arch}-unknown-linux-gnu.tar.gz" \
    && curl -fsSLO "https://github.com/googleworkspace/cli/releases/download/v${GWS_VERSION}/${gws}" \
    && curl -fsSLO "https://github.com/googleworkspace/cli/releases/download/v${GWS_VERSION}/${gws}.sha256" \
    && sha256sum --check "${gws}.sha256" \
    && tar --extract --file "${gws}" --directory /usr/local/bin ./gws \
    && apt-get purge --yes --auto-remove curl \
    && rm -rf /tmp/* /var/lib/apt/lists/*

# Renders the packet's PDF copies (PRD 04). Writer is the only component a `.docx` needs; the fonts
# are metric-compatible with Word's defaults, since a machine substitutes any font it lacks.
RUN apt-get update \
    && apt-get install --no-install-recommends --yes libreoffice-writer-nogui fonts-liberation \
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

# Only profile/search/ and profile/resume_skills.md (the inbox redline's skills list) survive .dockerignore (XC-11).
COPY profile ./profile

ENV PATH="/app/.venv/bin:$PATH"
USER jsa
ENTRYPOINT ["jsa", "cron"]
