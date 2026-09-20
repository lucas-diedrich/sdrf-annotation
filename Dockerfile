# from https://github.com/nezhar/claude-container/blob/97c7f99c56533ae6cee949f76bfd64a30d302be3/claude-code/Dockerfile
# Use official Node.js Debian image (bookworm-slim for better Python package compatibility)
FROM node:22-bookworm-slim


RUN apt-get update && apt-get install -y --no-install-recommends \
    git gosu bash python3 python3-pip python3-venv python3-dev \
    build-essential libfreetype6-dev libpng-dev cmake vim curl \
    ca-certificates gnupg bzip2 \
    && rm -rf /var/lib/apt/lists/*

# gh backs /sdrf-skills:sdrf-contribute, which forks, pushes a branch, and opens
# the PR against bigbio/sdrf-annotated-datasets via `gh api` / `gh pr create`.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

# Install uv (fast Python package manager) to system-wide location
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# Create and activate virtual environment, then install packages
RUN python3 -m venv /opt/venv
RUN chmod -R 777 /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# A full working tree, not just the plugin-cache copy: the spec submodule is two
# levels deep (--recursive is mandatory or templates.yaml is silently missing),
# and tools/cellline_db.py resolves data/ relative to its own __file__.
ENV SDRF_SKILLS_HOME=/opt/sdrf-skills
RUN git clone --recurse-submodules --shallow-submodules --depth 1 \
      https://github.com/bigbio/sdrf-skills.git $SDRF_SKILLS_HOME

# parse_sdrf (from sdrf-pipelines) backs the "validate before presenting" rule.
RUN uv pip install --python /opt/venv/bin/python -r $SDRF_SKILLS_HOME/requirements.txt

# techsdrf is not on PyPI or any conda channel. Installing straight from git
# fails: its pyproject declares both a PEP 639 `license = "Apache-2.0"` string
# and the superseded `License :: OSI Approved` classifier, which setuptools >=77
# rejects. Strip the classifier in a clone -- the clone-then-install fallback
# that requirements.txt itself documents. Drop this once upstream fixes it.
RUN git clone --depth 1 https://github.com/bigbio/techsdrf.git /tmp/techsdrf \
 && sed -i '/License :: OSI Approved/d' /tmp/techsdrf/pyproject.toml \
 && uv pip install --python /opt/venv/bin/python /tmp/techsdrf \
 && rm -rf /tmp/techsdrf

# ThermoRawFileParser gives /sdrf-skills:sdrf-techrefine its Thermo .raw support.
# Taken from the upstream release rather than bioconda: conda-forge's mono ships
# no managed assemblies on arm64 (no mscorlib.dll), so the conda build cannot
# run here, and the release zip is 3.6 MB against a 326 MB conda env.
# It is a .NET executable, so mono supplies the runtime on every architecture.
RUN apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
       mono-complete unzip \
    && rm -rf /var/lib/apt/lists/*
ARG TRFP_VERSION=1.4.5
RUN curl -fsSL -o /tmp/trfp.zip \
      "https://github.com/compomics/ThermoRawFileParser/releases/download/v${TRFP_VERSION}/ThermoRawFileParser${TRFP_VERSION}.zip" \
 && mkdir -p /opt/thermorawfileparser \
 && unzip -q /tmp/trfp.zip -d /opt/thermorawfileparser \
 && rm /tmp/trfp.zip \
 && printf '#!/bin/sh\nexec /usr/bin/mono /opt/thermorawfileparser/ThermoRawFileParser.exe "$@"\n' \
      > /usr/local/bin/ThermoRawFileParser \
 && chmod +x /usr/local/bin/ThermoRawFileParser \
 && ln -s /usr/local/bin/ThermoRawFileParser /usr/local/bin/thermorawfileparser

# Skills call `python -m tools`, never tools/*.py by path. PYTHONPATH keeps that
# working from /workspace while __file__ still points into the repo, so the
# cell-line DB under data/ resolves. A tools/ symlink would break that.
ENV PYTHONPATH=/opt/sdrf-skills

# Install Claude Code globally
RUN npm install -g @anthropic-ai/claude-code@2.1.278
# The plugin's bundled .mcp.json is dropped and the same server re-registered at
# user scope with absolute paths: its `./.venv/bin/python mcp/server.py` is
# relative to a repo root that is never the working directory here.
#
# Three remote servers cover the five tools the skills call that the bundled
# server does not implement: EBI's own OLS4 endpoint supplies the embedding
# search trio (searchClassesWithEmbeddingModel, listEmbeddingModels,
# searchWithEmbeddingModel), PubMed supplies search_articles and bioRxiv
# search_preprints. All three answer unauthenticated.
#
# Plugins are installed into a seed path rather than the default config dir:
# at runtime CLAUDE_CONFIG_DIR is a bind mount that would shadow anything baked
# into it, and the build-time default (/root/.claude) is unreadable after the
# entrypoint drops to a non-root user. entrypoint.sh copies the seed across.
ENV CLAUDE_SEED=/opt/claude-seed
RUN CLAUDE_CONFIG_DIR=$CLAUDE_SEED claude plugin marketplace add bigbio/sdrf-skills \
 && CLAUDE_CONFIG_DIR=$CLAUDE_SEED claude plugin install sdrf-skills@sdrf-skills \
 && find $CLAUDE_SEED -name plugin.json -path "*cache*" -exec node -e \
      'const f=process.argv[1],fs=require("fs");const j=JSON.parse(fs.readFileSync(f,"utf8"));delete j.hooks;fs.writeFileSync(f,JSON.stringify(j,null,2))' {} \; \
 && find $CLAUDE_SEED -path "*cache*" -name ".mcp.json" -delete \
 && CLAUDE_CONFIG_DIR=$CLAUDE_SEED claude mcp add -s user sdrf-pride-pmc \
      -- /opt/venv/bin/python $SDRF_SKILLS_HOME/mcp/server.py \
 && CLAUDE_CONFIG_DIR=$CLAUDE_SEED claude mcp add -s user --transport http ols \
      https://www.ebi.ac.uk/ols4/api/mcp \
 && CLAUDE_CONFIG_DIR=$CLAUDE_SEED claude mcp add -s user --transport http pubmed \
      https://pubmed.mcp.claude.com/mcp \
 && CLAUDE_CONFIG_DIR=$CLAUDE_SEED claude mcp add -s user --transport http biorxiv \
      https://hcls.mcp.claude.com/biorxiv/mcp \
 && chmod -R a+rX $CLAUDE_SEED

# Create directories
RUN mkdir -p /.claude /workspace

# Copy entrypoint script
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Set working directory
WORKDIR /workspace

# Set entrypoint
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

CMD ["sh", "-c", "echo 'Claude Code container is ready!'"]

# from https://github.com/nezhar/claude-container
# docker build -t claudeomics-benchmark .

# run once:
# docker run --rm -it -v "$(pwd)/claude_new:/claude" -e "CLAUDE_CONFIG_DIR=/claude" claudeomics-benchmark claude

# docker run --rm -it -v "$(pwd):/workspace" -v "$HOME/.config/claudeomics-benchmark-container:/claude" -e "CLAUDE_CONFIG_DIR=/claude" claudeomics-benchmark claude
