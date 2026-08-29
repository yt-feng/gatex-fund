#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 077

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
runner_base="${RUNNER_TEMP:-/tmp}"
work_dir="$(mktemp -d "$runner_base/intelligence-profile.XXXXXX")"
profile_key="${INTELLIGENCE_SOURCE_PROFILE:-}"

cleanup() {
  if [[ "$work_dir" == "$runner_base"/intelligence-profile.* ]]; then
    find "$work_dir" -type f -exec sh -c 'printf "" > "$1"' _ {} \; 2>/dev/null || true
    find "$work_dir" -depth -delete 2>/dev/null || true
  fi
}
trap cleanup EXIT

if [[ ! "$profile_key" =~ ^source-[a-z0-9-]{1,32}$ ]]; then
  echo "stage=profile-verification status=failed reason=profile-invalid" >&2
  exit 1
fi
: "${RUNTIME_AGE_IDENTITY:?runtime identity is required}"

profile_root="$repo_root/sealed/intelligence-sources/$profile_key"
python3 "$repo_root/scripts/install_age.py" --bin-dir "$work_dir/bin"
age_bin="$work_dir/bin/age"
printf '%s\n' "$RUNTIME_AGE_IDENTITY" > "$work_dir/runtime.identity"
chmod 600 "$work_dir/runtime.identity"
"$age_bin" -d -i "$work_dir/runtime.identity" \
  -o "$work_dir/runtime-config.json" "$profile_root/runtime-config.json.age"
unset RUNTIME_AGE_IDENTITY
printf '' > "$work_dir/runtime.identity"

PYTHONPATH="$repo_root/src" python3 -m intelligence_sources.cli verify-profile \
  --config "$work_dir/runtime-config.json" \
  --base-url "${TIKHUB_API_BASE:-https://api.tikhub.io}"
