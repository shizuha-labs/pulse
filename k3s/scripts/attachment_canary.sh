#!/usr/bin/env bash
# PLS-786 / PLS-483 (retro 2026-07-13): authenticated attachment-storage canary.
#
# Exercises the full authenticated round-trip against a live Pulse:
#   1. CREATE  — POST /api/attachments/ (item + multipart file, seat bearer)
#   2. READ    — GET /api/attachments/?item=<KEY> (list shows it)
#                GET /api/attachments/<id>/ (detail resolvable)
#                GET /api/attachments/<id>/serve/ (bearer-authenticated bytes)
#   3. DELETE  — DELETE /api/attachments/<id>/ (creator deletes own attachment)
#   4. VERIFY  — exact ID absent from a complete validated list
#
# Exit 0 = round-trip green. Failures report stage/known canary ID, not bodies
# or credentials. Partial pagination fails closed. PREREQ: localhost:18002 forwarded to pulse
# (kubectl -n shizuha port-forward svc/shizuha-pulse 18002:8002) and
# ~/.shizuha/auth/token-san.json holding {"accessToken": ...}.
set -euo pipefail

ITEM="${1:-PLS-786}"
BASE="http://localhost:18002/api"
BEAR=$(python3 -I -c 'import json,pathlib; print(json.loads((pathlib.Path.home()/".shizuha/auth/token-san.json").read_text())["accessToken"])')
WORK=$(mktemp -d)
trap 'rm -rf -- "$WORK"' EXIT
PAYLOAD="$WORK/payload.txt"
printf 'canary-payload-%s\n' "$(date +%s)" > "$PAYLOAD"
ATT_ID=""
fail() { echo "❌ CANARY FAILED at $1 (attachment=${ATT_ID:-not-created}); inspect/clean up this exact canary if created"; exit 1; }

# Never follow redirects with the bearer or trust an error response as JSON.
request() {
  local stage="$1" output="$2" expected="$3" code
  shift 3
  code=$(curl -sS -m 10 -H "Authorization: Bearer $BEAR" \
    -o "$output" -w '%{http_code}' "$@") || fail "$stage (network)"
  case " $expected " in *" $code "*) ;; *) fail "$stage (HTTP $code)" ;; esac
}

# Pagination is conservatively rejected: one partial page cannot prove absence.
check_list() {
  python3 -I - "$1" "$ATT_ID" "$2" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
if isinstance(value, dict):
    assert 'results' in value and 'next' in value and value['next'] is None
    rows = value['results']
    assert type(value.get('count')) is int and value['count'] == len(rows)
else:
    rows = value
assert isinstance(rows, list)
assert all(isinstance(row, dict) and isinstance(row.get('id'), str) for row in rows)
found = any(row['id'] == sys.argv[2] for row in rows)
assert found == (sys.argv[3] == 'present')
PY
}

echo "== 1. CREATE (authed POST, item=$ITEM)"
request create "$WORK/create.json" 201 -X POST \
  --form-string "item=${ITEM}" -F "file=@${PAYLOAD}" "${BASE}/attachments/"
ATT_ID=$(python3 -I - "$WORK/create.json" <<'PY'
import json, sys, uuid
value = json.load(open(sys.argv[1]))['id']
assert isinstance(value, str) and str(uuid.UUID(value)) == value
print(value)
PY
) || fail 'create (invalid ID)'
echo "   created: $ATT_ID"

echo "== 2. READ (list shows exact ID)"
request list "$WORK/list.json" 200 -G --data-urlencode "item=$ITEM" "${BASE}/attachments/"
check_list "$WORK/list.json" present || fail 'read (invalid/incomplete list or missing ID)'

echo "== 2b. READ (detail resolvable)"
request detail "$WORK/detail.json" 200 "${BASE}/attachments/${ATT_ID}/"
python3 -I - "$WORK/detail.json" "$ATT_ID" <<'PY' || fail 'read (invalid detail)'
import json, sys
value = json.load(open(sys.argv[1]))
assert value['id'] == sys.argv[2] and isinstance(value['storage_key'], str) and value['storage_key']
PY

echo "== 2c. READ (authenticated exact bytes)"
# Canonical same-origin proxy supports bearer auth; no signed URL or redirect
# is needed, and no server-supplied URL can receive the seat credential.
request download "$WORK/download.bin" 200 "${BASE}/attachments/${ATT_ID}/serve/"
cmp -s "$PAYLOAD" "$WORK/download.bin" || fail 'read (payload mismatch)'

echo "== 3. DELETE (creator deletes own attachment)"
request delete "$WORK/delete.out" '200 204' -X DELETE "${BASE}/attachments/${ATT_ID}/"

echo "== 4. VERIFY (exact ID absent from complete list)"
request verify "$WORK/after.json" 200 -G --data-urlencode "item=$ITEM" "${BASE}/attachments/"
check_list "$WORK/after.json" absent || fail 'verify (invalid/incomplete list or ID still present)'
echo "   gone ✓"
echo "✅ ATTACHMENT CANARY PASS — authenticated create/read/delete round-trip green"
