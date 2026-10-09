FROM python:3.14-slim-trixie

# Python reads timezone rules from the system, and the slim image ships none.
RUN apt-get update \
    && apt-get install --no-install-recommends --yes tzdata \
    && rm -rf /var/lib/apt/lists/*

# The inbox's packet build shells out to these (PRD 06). Pinned releases; the musl `typst` is static.
ARG GWS_VERSION=0.22.5
ARG PANDOC_VERSION=3.12.1
ARG TYPST_VERSION=0.15.1
RUN apt-get update \
    && apt-get install --no-install-recommends --yes ca-certificates curl xz-utils \
    && arch="$(uname -m)" \
    && deb_arch="$(dpkg --print-architecture)" \
    && cd /tmp \
    && gws="google-workspace-cli-${arch}-unknown-linux-gnu.tar.gz" \
    && curl -fsSLO "https://github.com/googleworkspace/cli/releases/download/v${GWS_VERSION}/${gws}" \
    && curl -fsSLO "https://github.com/googleworkspace/cli/releases/download/v${GWS_VERSION}/${gws}.sha256" \
    && sha256sum --check "${gws}.sha256" \
    && tar --extract --file "${gws}" --directory /usr/local/bin ./gws \
    && curl -fsSLO "https://github.com/jgm/pandoc/releases/download/${PANDOC_VERSION}/pandoc-${PANDOC_VERSION}-1-${deb_arch}.deb" \
    && apt-get install --yes "./pandoc-${PANDOC_VERSION}-1-${deb_arch}.deb" \
    && curl -fsSL "https://github.com/typst/typst/releases/download/v${TYPST_VERSION}/typst-${arch}-unknown-linux-musl.tar.xz" \
        | tar --extract --xz --strip-components=1 --directory /usr/local/bin --wildcards '*/typst' \
    && rm -rf /tmp/* /var/lib/apt/lists/*

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
