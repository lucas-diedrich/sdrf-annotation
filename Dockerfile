# from https://github.com/nezhar/claude-container/blob/97c7f99c56533ae6cee949f76bfd64a30d302be3/claude-code/Dockerfile
# Use official Node.js Debian image (bookworm-slim for better Python package compatibility)
FROM node:22-bookworm-slim


RUN apt-get update && apt-get install -y --no-install-recommends \
    git gosu bash python3 python3-pip python3-venv python3-dev \
    build-essential libfreetype6-dev libpng-dev cmake vim curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv (fast Python package manager) to system-wide location
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh                            

# Create and activate virtual environment, then install packages
RUN python3 -m venv /opt/venv
RUN chmod -R 777 /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Claude Code globally
RUN npm install -g @anthropic-ai/claude-code@2.1.278
RUN claude plugin marketplace add bigbio/sdrf-skills && claude plugin install sdrf-skills@sdrf-skills

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
