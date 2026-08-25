#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 077

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
runner_base="${RUNNER_TEMP:-/tmp}"
work_dir="$(mktemp -d "$runner_base/snapshot-pipeline.XXXXXX")"

cleanup() {
  rm -rf -- "$work_dir"
}
trap cleanup EXIT

: "${RUNTIME_AGE_IDENTITY:?runtime identity is required}"
: "${SOURCE_API_TOKEN:?source credential is required}"

python3 "$repo_root/scripts/install_age.py" --bin-dir "$work_dir/bin"
age_bin="$work_dir/bin/age"

printf '%s\n' "$RUNTIME_AGE_IDENTITY" > "$work_dir/runtime.identity"
chmod 600 "$work_dir/runtime.identity"

"$age_bin" -d -i "$work_dir/runtime.identity" -o "$work_dir/runtime-config.json" "$repo_root/sealed/runtime-config.json.age"
"$age_bin" -d -i "$work_dir/runtime.identity" -o "$work_dir/provider-overlay.py" "$repo_root/sealed/provider-overlay.py.age"
"$age_bin" -d -i "$work_dir/runtime.identity" -o "$work_dir/state.json" "$repo_root/sealed/checkpoint.json.age"

PYTHONPATH="$repo_root/src" python3 -m snapshot_pipeline.cli run \
  --config "$work_dir/runtime-config.json" \
  --provider "$work_dir/provider-overlay.py" \
  --state "$work_dir/state.json" \
  --state-out "$work_dir/state.next.json" \
  --work "$work_dir/run" \
  --result "$work_dir/result.json"

new_count="$(python3 -c 'import json,sys; print(int(json.load(open(sys.argv[1]))["new_count"]))' "$work_dir/result.json")"
state_changed="$(python3 -c 'import json,sys; print("1" if json.load(open(sys.argv[1]))["state_changed"] else "0")' "$work_dir/result.json")"

if [[ "$state_changed" == "1" ]]; then
  "$age_bin" -R "$repo_root/recipients/runtime-recipient.txt" -o "$work_dir/checkpoint.json.age" "$work_dir/state.next.json"
  mv "$work_dir/checkpoint.json.age" "$repo_root/sealed/checkpoint.json.age"
fi

if [[ "$new_count" -gt 0 ]]; then
  PYTHONPATH="$repo_root/src" python3 -m snapshot_pipeline.cli pack \
    --source "$work_dir/run/batch" \
    --output "$work_dir/bundle.tar" \
    --id-file "$work_dir/bundle-id.txt"
  bundle_id="$(tr -d '\r\n' < "$work_dir/bundle-id.txt")"
  destination="$repo_root/vault/${bundle_id:0:2}/$bundle_id.tar.age"
  mkdir -p "$(dirname "$destination")"
  "$age_bin" -R "$repo_root/recipients/export-recipient.txt" -o "$destination" "$work_dir/bundle.tar"
fi

PYTHONPATH="$repo_root/src" python3 -m snapshot_pipeline.cli guard \
  --root "$repo_root" \
  --config "$work_dir/runtime-config.json"

git -C "$repo_root" add sealed/checkpoint.json.age vault
if git -C "$repo_root" diff --cached --quiet; then
  echo "stage=state status=empty count=0"
  exit 0
fi

git -C "$repo_root" config user.name "snapshot-pipeline[bot]"
git -C "$repo_root" config user.email "snapshot-pipeline[bot]@users.noreply.github.com"
git -C "$repo_root" commit -m "snapshot: update sealed ledger"
git -C "$repo_root" push origin "HEAD:${GITHUB_REF_NAME:-main}"
echo "stage=state status=ok count=$new_count"
