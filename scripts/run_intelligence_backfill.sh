#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 077

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
runner_base="${RUNNER_TEMP:-/tmp}"
work_dir="$(mktemp -d "$runner_base/intelligence-backfill.XXXXXX")"
age_bin=""
profile_key="${INTELLIGENCE_SOURCE_PROFILE:-}"
delivery_mode="${INTELLIGENCE_DELIVERY_MODE:-dry-run}"
maximum_items="${INTELLIGENCE_BACKFILL_MAXIMUM_ITEMS:-10}"
intake_secret="${GATEX_INTELLIGENCE_INTAKE_SECRET-}"
unset GATEX_INTELLIGENCE_INTAKE_SECRET

cleanup() {
  if [[ "$work_dir" == "$runner_base"/intelligence-backfill.* ]]; then
    find "$work_dir" -type f -exec sh -c 'printf "" > "$1"' _ {} \; 2>/dev/null || true
    find "$work_dir" -depth -delete 2>/dev/null || true
  fi
}
trap cleanup EXIT

if [[ ! "$profile_key" =~ ^source-[a-z0-9-]{1,32}$ ]]; then
  echo "stage=backfill status=failed reason=profile-invalid" >&2
  exit 1
fi
if [[ "$delivery_mode" != "dry-run" && "$delivery_mode" != "post" ]]; then
  echo "stage=backfill status=failed reason=delivery-mode-invalid" >&2
  exit 1
fi
if [[ ! "$maximum_items" =~ ^[0-9]+$ || "$maximum_items" -lt 1 || "$maximum_items" -gt 50 ]]; then
  echo "stage=backfill status=failed reason=maximum-items-invalid" >&2
  exit 1
fi

: "${RUNTIME_AGE_IDENTITY:?runtime identity is required}"
profile_root="$repo_root/sealed/intelligence-sources/$profile_key"
checkpoint_relative="sealed/intelligence-sources/$profile_key/backfill-checkpoint.json.age"

python3 "$repo_root/scripts/install_age.py" --bin-dir "$work_dir/bin"
age_bin="$work_dir/bin/age"
printf '%s\n' "$RUNTIME_AGE_IDENTITY" > "$work_dir/runtime.identity"
chmod 600 "$work_dir/runtime.identity"
"$age_bin" -d -i "$work_dir/runtime.identity" -o "$work_dir/runtime-config.json" "$profile_root/runtime-config.json.age"
"$age_bin" -d -i "$work_dir/runtime.identity" -o "$work_dir/state.json" "$profile_root/backfill-checkpoint.json.age"
unset RUNTIME_AGE_IDENTITY
printf '' > "$work_dir/runtime.identity"

PYTHONPATH="$repo_root/src" python3 -m intelligence_sources.cli inspect-config \
  --config "$work_dir/runtime-config.json" >/dev/null
if [[ "$delivery_mode" == "post" ]]; then
  GATEX_INTELLIGENCE_INTAKE_SECRET="$intake_secret" \
    PYTHONPATH="$repo_root/src" python3 -m intelligence_sources.cli check-delivery \
    --endpoint "${GATEX_INTELLIGENCE_INTAKE_URL:-}" >/dev/null
fi
PYTHONPATH="$repo_root/src" python3 -m intelligence_sources.cli backfill-page \
  --config "$work_dir/runtime-config.json" \
  --state "$work_dir/state.json" \
  --state-out "$work_dir/state.next.json" \
  --output "$work_dir/intake.jsonl" \
  --maximum-items "$maximum_items" \
  --base-url "${TIKHUB_API_BASE:-https://api.tikhub.io}"
unset TIKHUB_WECHAT_TOKEN
GATEX_INTELLIGENCE_INTAKE_SECRET="$intake_secret" \
  PYTHONPATH="$repo_root/src" python3 -m intelligence_sources.cli deliver \
  --input "$work_dir/intake.jsonl" \
  --mode "$delivery_mode" \
  --endpoint "${GATEX_INTELLIGENCE_INTAKE_URL:-}"
intake_secret=""

if [[ "$delivery_mode" == "dry-run" ]]; then
  echo "stage=backfill-state status=empty mode=dry-run count=0"
  exit 0
fi

"$age_bin" -R "$repo_root/recipients/runtime-recipient.txt" \
  -o "$work_dir/backfill-checkpoint.json.age" "$work_dir/state.next.json"
mv "$work_dir/backfill-checkpoint.json.age" "$profile_root/backfill-checkpoint.json.age"
PYTHONPATH="$repo_root/src" python3 -m snapshot_pipeline.cli guard \
  --root "$repo_root" --config "$work_dir/runtime-config.json"

git -C "$repo_root" add -- "$checkpoint_relative"
if git -C "$repo_root" diff --cached --quiet; then
  echo "stage=backfill-state status=empty count=0"
  exit 0
fi
git -C "$repo_root" config user.name "snapshot-pipeline[bot]"
git -C "$repo_root" config user.email "snapshot-pipeline[bot]@users.noreply.github.com"
git -C "$repo_root" commit -m "snapshot: update sealed backfill cursor" -- "$checkpoint_relative"
git -C "$repo_root" push origin "HEAD:${GITHUB_REF_NAME:-main}"
echo "stage=backfill-state status=ok count=1"
