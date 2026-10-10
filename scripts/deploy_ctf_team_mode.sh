#!/bin/sh
set -eu

project_name=bbx-ctf-team-mode
env_file=${BBX_CTF_TEAM_MODE_ENV_FILE:-/Users/yym/blackboard-explorer/.env}

exec docker compose \
  --project-name "$project_name" \
  --env-file "$env_file" \
  -f docker-compose.yml \
  -f docker-compose.ctf-team-mode.yml \
  "$@"
