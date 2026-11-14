#!/usr/bin/env bash
# Shared idempotent persistence for every encrypted source cursor.
push_sealed_state() {
  local repo_root="$1" target_branch="$2" push_attempt
  for push_attempt in 1 2 3 4 5; do
    if git -C "$repo_root" push origin "HEAD:$target_branch"; then return 0; fi
    # An error may follow an accepted write. Observe before any retry.
    if git -C "$repo_root" fetch origin "$target_branch"; then
      if git -C "$repo_root" merge-base --is-ancestor HEAD FETCH_HEAD; then return 0; fi
      git -C "$repo_root" rebase FETCH_HEAD
    fi
    if [[ "$push_attempt" -lt 5 ]]; then
      echo "stage=state status=retry attempt=$push_attempt"
      sleep $((5 * (2 ** (push_attempt - 1))))
    fi
  done
  echo "stage=state status=failed reason=push-exhausted" >&2
  return 1
}
