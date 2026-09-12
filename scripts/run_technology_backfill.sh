#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 077

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
runner_base="${RUNNER_TEMP:-/tmp}"
work_dir="$(mktemp -d "$runner_base/technology-backfill.XXXXXX")"
age_bin=""
delivery_mode="${TECHNOLOGY_BACKFILL_MODE:-post}"
maximum_items="${TECHNOLOGY_BACKFILL_MAXIMUM_ITEMS:-20}"
frontier_secret="${GATEX_TECHNOLOGY_PUBLICATION_SECRET-}"
unset GATEX_TECHNOLOGY_PUBLICATION_SECRET

cleanup() {
  if [[ "$work_dir" == "$runner_base"/technology-backfill.* ]]; then
    find "$work_dir" -type f -exec sh -c 'printf "" > "$1"' _ {} \; 2>/dev/null || true
    find "$work_dir" -depth -delete 2>/dev/null || true
  fi
}
trap cleanup EXIT

if [[ "$delivery_mode" != "dry-run" && "$delivery_mode" != "post" ]]; then
  echo "stage=technology-backfill status=failed reason=delivery-mode-invalid" >&2
  exit 1
fi
if [[ ! "$maximum_items" =~ ^[0-9]+$ || "$maximum_items" -lt 1 || "$maximum_items" -gt 50 ]]; then
  echo "stage=technology-backfill status=failed reason=maximum-items-invalid" >&2
  exit 1
fi

: "${RUNTIME_AGE_IDENTITY:?runtime identity is required}"
if [[ "$delivery_mode" == "post" ]]; then
  : "${frontier_secret:?Technology publication credential is required}"
fi

profile_root="$repo_root/sealed/intelligence-sources/source-a"
state_relative="sealed/intelligence-sources/source-a/technology-backfill-checkpoint.json.age"

python3 "$repo_root/scripts/install_age.py" --bin-dir "$work_dir/bin"
age_bin="$work_dir/bin/age"
printf '%s\n' "$RUNTIME_AGE_IDENTITY" > "$work_dir/runtime.identity"
chmod 600 "$work_dir/runtime.identity"
"$age_bin" -d -i "$work_dir/runtime.identity" -o "$work_dir/runtime-config.json" "$profile_root/runtime-config.json.age"
"$age_bin" -d -i "$work_dir/runtime.identity" -o "$work_dir/state.json" "$profile_root/technology-backfill-checkpoint.json.age"
unset RUNTIME_AGE_IDENTITY

seed_pending="$(python3 - "$work_dir/state.json" <<'PY'
import json, sys
state = json.load(open(sys.argv[1], encoding="utf-8"))
print("1" if state.get("seedPending") else "0")
PY
)"
: > "$work_dir/sources.jsonl"
if [[ "$seed_pending" == "1" ]]; then
  "$age_bin" -d -i "$work_dir/runtime.identity" \
    -o "$work_dir/seed.jsonl" "$profile_root/technology-seed.jsonl.age"
  cat "$work_dir/seed.jsonl" >> "$work_dir/sources.jsonl"
fi

if [[ -n "${TIKHUB_WECHAT_TOKEN:-}" ]]; then
  PYTHONPATH="$repo_root/src" python3 -m intelligence_sources.cli technology-backfill-page \
    --config "$work_dir/runtime-config.json" \
    --state "$work_dir/state.json" \
    --state-out "$work_dir/state.next.json" \
    --output "$work_dir/tikhub.jsonl" \
    --maximum-items "$maximum_items" \
    --base-url "${TIKHUB_API_BASE:-https://api.tikhub.io}"
  cat "$work_dir/tikhub.jsonl" >> "$work_dir/sources.jsonl"
else
  if [[ "$seed_pending" != "1" ]]; then
    echo "stage=technology-backfill status=skipped reason=tikhub-credential-unavailable"
    exit 0
  fi
  cp "$work_dir/state.json" "$work_dir/state.next.json"
fi
printf '' > "$work_dir/runtime.identity"
unset TIKHUB_WECHAT_TOKEN

if [[ "$seed_pending" == "1" ]]; then
  python3 - "$work_dir/state.next.json" <<'PY'
import json, sys
path = sys.argv[1]
state = json.load(open(path, encoding="utf-8"))
state["seedPending"] = False
json.dump(state, open(path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
open(path, "a", encoding="utf-8").write("\n")
PY
fi

prepared_count="$(wc -l < "$work_dir/sources.jsonl" | tr -d '[:space:]')"
if [[ "$delivery_mode" == "dry-run" ]]; then
  echo "stage=technology-backfill-state status=empty mode=dry-run count=$prepared_count"
  exit 0
fi

GATEX_TECHNOLOGY_PUBLICATION_SECRET="$frontier_secret" \
  python3 "$repo_root/scripts/technology_frontiers_daily.py" enqueue-jsonl --input "$work_dir/sources.jsonl"
frontier_secret=""

"$age_bin" -R "$repo_root/recipients/runtime-recipient.txt" \
  -o "$work_dir/technology-backfill-checkpoint.json.age" "$work_dir/state.next.json"
mv "$work_dir/technology-backfill-checkpoint.json.age" "$profile_root/technology-backfill-checkpoint.json.age"

PYTHONPATH="$repo_root/src" python3 -m snapshot_pipeline.cli guard \
  --root "$repo_root" --config "$work_dir/runtime-config.json"

git -C "$repo_root" add -- "$state_relative"
if git -C "$repo_root" diff --cached --quiet; then
  echo "stage=technology-backfill-state status=empty count=$prepared_count"
  exit 0
fi
git -C "$repo_root" config user.name "snapshot-pipeline[bot]"
git -C "$repo_root" config user.email "snapshot-pipeline[bot]@users.noreply.github.com"
git -C "$repo_root" commit -m "snapshot: update Technology Frontiers backfill cursor" -- "$state_relative"
git -C "$repo_root" push origin "HEAD:${GITHUB_REF_NAME:-main}"
echo "stage=technology-backfill-state status=ok count=$prepared_count"
