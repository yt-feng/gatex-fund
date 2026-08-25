#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 077

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
runner_base="${RUNNER_TEMP:-/tmp}"
work_dir="$(mktemp -d "$runner_base/snapshot-pipeline.XXXXXX")"

cleanup() {
  if [[ -s "$work_dir/egress.pid" ]]; then
    local egress_pid
    read -r egress_pid < "$work_dir/egress.pid"
    if [[ "$egress_pid" =~ ^[0-9]+$ ]]; then
      kill -TERM -- "-$egress_pid" 2>/dev/null || true
      for _ in {1..20}; do
        if ! kill -0 "$egress_pid" 2>/dev/null; then
          break
        fi
        sleep 0.1
      done
      if kill -0 "$egress_pid" 2>/dev/null; then
        kill -KILL -- "-$egress_pid" 2>/dev/null || true
      fi
    fi
  fi
  rm -rf -- "$work_dir"
}
trap cleanup EXIT

: "${RUNTIME_AGE_IDENTITY:?runtime identity is required}"

python3 "$repo_root/scripts/install_age.py" --bin-dir "$work_dir/bin"
age_bin="$work_dir/bin/age"

printf '%s\n' "$RUNTIME_AGE_IDENTITY" > "$work_dir/runtime.identity"
chmod 600 "$work_dir/runtime.identity"

"$age_bin" -d -i "$work_dir/runtime.identity" -o "$work_dir/runtime-config.json" "$repo_root/sealed/runtime-config.json.age"
"$age_bin" -d -i "$work_dir/runtime.identity" -o "$work_dir/provider-overlay.py" "$repo_root/sealed/provider-overlay.py.age"
"$age_bin" -d -i "$work_dir/runtime.identity" -o "$work_dir/state.json" "$repo_root/sealed/checkpoint.json.age"
unset RUNTIME_AGE_IDENTITY
rm -f -- "$work_dir/runtime.identity"

if [[ -n "${EGRESS_PROXY_URI:-}" ]]; then
  egress_args=(
    --work-dir "$work_dir/egress"
    --address-file "$work_dir/egress.address"
    --pid-file "$work_dir/egress.pid"
  )
  if [[ -n "${SOCKS5_CLIENT_BIN:-}" ]]; then
    egress_args+=(--client-bin "$SOCKS5_CLIENT_BIN")
  fi
  python3 "$repo_root/scripts/start_socks5_egress.py" "${egress_args[@]}"
  proxy_address="$(tr -d '\r\n' < "$work_dir/egress.address")"
  unset EGRESS_PROXY_URI
  export SNAPSHOT_EGRESS_PROXY="$proxy_address"
  export ALL_PROXY="$proxy_address"
  export HTTPS_PROXY="$proxy_address"
  export HTTP_PROXY="$proxy_address"
  export all_proxy="$proxy_address"
  export https_proxy="$proxy_address"
  export http_proxy="$proxy_address"
  export NO_PROXY="127.0.0.1,localhost"
  export no_proxy="$NO_PROXY"
fi

token_env="$(python3 - "$work_dir/runtime-config.json" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    value = json.load(handle).get("token_env")
if value is None:
    print("")
elif isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", value):
    print(value)
else:
    raise SystemExit("runtime token binding is invalid")
PY
)"
if [[ -n "$token_env" ]]; then
  token_value="${!token_env-}"
  if [[ -z "$token_value" ]]; then
    echo "stage=run status=failed reason=credential-unavailable" >&2
    exit 1
  fi
  unset token_value
fi

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
git -C "$repo_root" commit -m "snapshot: update sealed ledger" -- sealed/checkpoint.json.age vault
git -C "$repo_root" push origin "HEAD:${GITHUB_REF_NAME:-main}"
echo "stage=state status=ok count=$new_count"
