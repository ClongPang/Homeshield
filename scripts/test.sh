#!/usr/bin/env bash
set -euo pipefail

docker compose up -d --wait postgres
uv run pytest
