#!/usr/bin/env bash
# Gate a deploy on the live service answering as expected (RC1-459). A green
# `sam deploy` or `vercel --prod` is not proof the code runs: a Lambda that
# fails at import deploys cleanly and then 500s on every request.
#
# Three consecutive matches within 180 s, not the first one, so a service that
# answers once and then falls over does not pass.
#
# Usage: wait_for_http.sh <GET|POST> <url> <expected_status>
set -uo pipefail

method="$1"
url="$2"
expected="$3"

deadline=$((SECONDS + 180))
ok=0
while [ "$ok" -lt 3 ]; do
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "::error::$method $url never returned $expected for 3 consecutive checks within 180s"
    exit 1
  fi
  if [ "$method" = "POST" ]; then
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 30 -X POST -H 'Content-Type: application/json' -d '{}' "$url")
  else
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 30 "$url")
  fi
  if [ "$code" = "$expected" ]; then
    ok=$((ok + 1))
    echo "$code as expected ($ok/3)"
  else
    ok=0
    echo "got $code, want $expected"
  fi
  [ "$ok" -ge 3 ] || sleep 10
done
echo "confirmed: $method $url -> $expected"
