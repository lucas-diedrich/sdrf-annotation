# README

Auto-annotation of proteomics datasets with SDRF files.

## Workflow 

This repository implements the workflow proposed by the `bigbio/sdrf-skills` repository and implements it in a Docker container. 

## Setup

Build the docker container

```bash
docker build --no-cache -t sdrf-annotation .
rm -rf $(pwd)/claude_credentials_setup && mkdir $(pwd)/claude_credentials_setup
docker run --rm -it -v "$(pwd)/claude_credentials_setup:/.claude" -e "CLAUDE_CONFIG_DIR=/.claude" -e "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" sdrf-annotation claude
```