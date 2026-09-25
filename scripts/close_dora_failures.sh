#!/usr/bin/env bash
# Close the DORA change failures this deploy restores (RC1-448 / RC1-464). A
# failure opened by report_dora_failure.sh is keyed <service>.<failing sha>;
# POSTing that id again with finished_at set replaces the record and stamps
# time to restore. The walk covers every failed run of this workflow back to
# the previous success, plus this run's own earlier attempts after a re-run
# to green (which `gh run list` hides). Each candidate is checked for a
# failed gate step first: the API upserts by id, so POSTing the id of a run
# that failed before its gate (tests, the deploy command) would fabricate a
# change failure that never happened. Stopping at the previous success also
# matters — re-closing an already closed failure would overwrite its
# finished_at and inflate its time to restore.
#
# Usage: close_dora_failures.sh <service> <workflow_file> <gate step name>...
# Env:   DD_API_KEY; GH_TOKEN with actions: read; the GITHUB_* defaults.
#        CURRENT_SHA overrides GITHUB_SHA when the workflow deploys a
#        different commit than the event sha (tag pushes, workflow_run).
set -euo pipefail

service="$1"
workflow_file="$2"
shift 2
GATE_STEPS=$(printf '%s\n' "$@")
export GATE_STEPS

current_sha="${CURRENT_SHA:-$GITHUB_SHA}"
repo_url="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}"
finished=$(date +%s)

# When one of the gate steps failed in one run attempt, empty if none did.
gate_failed_at() {
  gh api "repos/$GITHUB_REPOSITORY/actions/runs/$1/attempts/$2/jobs" \
    --jq '(env.GATE_STEPS | split("\n") | map(select(length > 0))) as $gates
          | [.jobs[].steps[] | select((.name as $n | $gates | index($n)) and .conclusion == "failure")][0].completed_at // empty'
}

close_failure() { # $1 = failing sha, $2 = gate-failure epoch seconds
  payload=$(printf '{"data":{"attributes":{"id":"%s.%s","services":["%s"],"env":"prod","severity":"High","name":"%s deploy %.8s failed its deploy gate — restored by %.8s","started_at":%s000000000,"finished_at":%s000000000,"git":{"commit_sha":"%s","repository_url":"%s"}}}}' \
    "$service" "$1" "$service" "$service" "$1" "$current_sha" "$2" "$finished" "$1" "$repo_url")
  code=$(curl -s -o dora-response.json -w '%{http_code}' -m 30 -X POST \
    -H "DD-API-KEY: $DD_API_KEY" -H "Content-Type: application/json" \
    -d "$payload" "https://api.datadoghq.com/api/v2/dora/failure")
  cat dora-response.json; echo
  case "$code" in
    2*) echo "closed DORA change failure $service.$1 — restored after $((finished - $2))s";;
    *) echo "::error::close POST for $1 failed with HTTP $code. Do NOT re-run this workflow to retry — that double-counts the deployment event (DORA never dedups). Re-send the close by hand."; exit 1;;
  esac
}

# This run's own earlier attempts (a re-run to green shares the sha).
if [ "${GITHUB_RUN_ATTEMPT:-1}" -gt 1 ]; then
  a=1
  while [ "$a" -lt "$GITHUB_RUN_ATTEMPT" ]; do
    t=$(gate_failed_at "$GITHUB_RUN_ID" "$a")
    if [ -n "$t" ]; then
      close_failure "$current_sha" "$(date -d "$t" +%s)"
      break
    fi
    a=$((a + 1))
  done
fi

# Completed runs of this workflow, newest first; the streak of failures since
# the previous success is what this deploy restores. No branch filter: the
# workflow's own trigger already constrains the run set (pushes to main, or
# v* tags for a release workflow). 50 deep because a workflow_run-triggered
# workflow accumulates skipped rows for every PR run of its trigger.
gh api "repos/$GITHUB_REPOSITORY/actions/workflows/$workflow_file/runs?status=completed&per_page=50" \
  --jq '.workflow_runs | map(select(.id != (env.GITHUB_RUN_ID | tonumber)))
        | (map(.conclusion) | index("success")) as $cut
        | (if $cut == null then . else .[:$cut] end)
        | map(select(.conclusion == "failure") | [.id, .head_sha, .run_attempt] | @tsv)
        | .[]' > dora-streak.tsv

if [ ! -s dora-streak.tsv ]; then
  echo "no failed runs since the previous success — nothing to close for $service"
  exit 0
fi

while IFS=$'\t' read -r rid sha attempts; do
  t=""
  a=1
  while [ "$a" -le "$attempts" ]; do
    at=$(gate_failed_at "$rid" "$a")
    if [ -n "$at" ] && [ -z "$t" ]; then t="$at"; fi
    a=$((a + 1))
  done
  if [ -z "$t" ]; then
    echo "run $rid ($sha) failed before its gate — no change failure to close for $service"
    continue
  fi
  close_failure "$sha" "$(date -d "$t" +%s)"
done < dora-streak.tsv
