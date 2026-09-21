#!/usr/bin/env bash
# Register one deploy with Datadog DORA (RC1-459). Called by deploy.yml after a
# deploy's health gate passes, so a deployment event means the code serves, not
# merely that the deploy command exited 0.
#
# DORA deployments are their own data source, separate from CI Visibility:
# deployment frequency and lead time are computed from these events, and lead
# time also needs commit_sha + repository_url to join the git metadata.
# Timestamps are nanoseconds. A failed POST fails the workflow on purpose: the
# deploy is live either way, but an unrecorded deploy silently undercounts.
#
# Usage: report_dora_deployment.sh <service> <started_at_epoch_seconds>
# Env:   DD_API_KEY, GITHUB_SHA, GITHUB_SERVER_URL, GITHUB_REPOSITORY
set -euo pipefail

service="$1"
started_at="$2"
repo_url="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}"

payload=$(printf '{"data":{"attributes":{"service":"%s","env":"prod","started_at":%s000000000,"finished_at":%s000000000,"git":{"commit_sha":"%s","repository_url":"%s"}}}}' \
  "$service" "$started_at" "$(date +%s)" "$GITHUB_SHA" "$repo_url")
code=$(curl -s -o dora-response.json -w '%{http_code}' -m 30 -X POST \
  -H "DD-API-KEY: $DD_API_KEY" -H "Content-Type: application/json" \
  -d "$payload" "https://api.datadoghq.com/api/v2/dora/deployment")
cat dora-response.json; echo
case "$code" in
  2*) echo "DORA deployment recorded for $service (HTTP $code)";;
  *) echo "::error::DORA deployment POST for $service failed with HTTP $code — this deploy is live but unrecorded"; exit 1;;
esac
