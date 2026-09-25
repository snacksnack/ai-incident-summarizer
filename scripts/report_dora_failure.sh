#!/usr/bin/env bash
# Open a DORA change failure for this deploy (RC1-448 / RC1-464). Called by the
# deploy workflow when its health gate fails: the deploy completed but the
# service does not serve, which is the defining condition of a change failure.
# A run where the deploy command itself failed must NOT call this — nothing
# shipped, so that is a failed deployment, not a change failure.
#
# The event is keyed id=<service>.<sha>. The next deploy that passes the gate
# closes it by re-POSTing the same id with finished_at set
# (scripts/close_dora_failures.sh): the DORA failure API upserts by id.
#
# Usage: report_dora_failure.sh <service> [sha]   (sha defaults to GITHUB_SHA)
# Env:   DD_API_KEY, GITHUB_SHA, GITHUB_SERVER_URL, GITHUB_REPOSITORY
set -euo pipefail

service="$1"
sha="${2:-$GITHUB_SHA}"
repo_url="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}"

payload=$(printf '{"data":{"attributes":{"id":"%s.%s","services":["%s"],"env":"prod","severity":"High","name":"%s deploy %.8s failed its deploy gate","started_at":%s000000000,"git":{"commit_sha":"%s","repository_url":"%s"}}}}' \
  "$service" "$sha" "$service" "$service" "$sha" "$(date +%s)" "$sha" "$repo_url")
code=$(curl -s -o dora-response.json -w '%{http_code}' -m 30 -X POST \
  -H "DD-API-KEY: $DD_API_KEY" -H "Content-Type: application/json" \
  -d "$payload" "https://api.datadoghq.com/api/v2/dora/failure")
cat dora-response.json; echo
case "$code" in
  2*) echo "DORA change failure opened for $service (HTTP $code); the next passing deploy closes it";;
  *) echo "::error::DORA failure POST for $service failed with HTTP $code — change failure rate will undercount"; exit 1;;
esac
