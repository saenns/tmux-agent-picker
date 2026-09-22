#!/usr/bin/env bash
set -u

plugin_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
hosts=$(tmux show-option -gqv @agent-picker-hosts)
timeout=$(tmux show-option -gqv @agent-picker-ssh-timeout)
batch_mode=$(tmux show-option -gqv @agent-picker-ssh-batch-mode)
remote_tmux=$(tmux show-option -gqv @agent-picker-remote-tmux)
retries=$(tmux show-option -gqv @agent-picker-ssh-retries)
interval=$(tmux show-option -gqv @agent-picker-refresh-interval)
timeout=${timeout:-2}
batch_mode=${batch_mode:-yes}
retries=${retries:-0}
interval=${interval:-15}
cache_root=${XDG_CACHE_HOME:-$HOME/.cache}/tmux-agent-picker
cache_key=$(printf '%s' "$hosts|$timeout|$batch_mode|$remote_tmux|$retries" | shasum -a 256 | awk '{print $1}')

refresh() {
  "$plugin_dir/scripts/inventory.py" \
    --hosts "$hosts" \
    --timeout "$timeout" \
    --ssh-batch-mode "$batch_mode" \
    --remote-tmux "$remote_tmux" \
    --ssh-retries "$retries" \
    --cache-file "$cache_root/inventory-$cache_key.json" \
    --refresh-cache >/dev/null 2>&1
}

if [[ ${1:-} != --watch ]]; then
  refresh
  exit 0
fi

mkdir -p "$cache_root"
lock_dir="$cache_root/refresh.lock"
mkdir "$lock_dir" 2>/dev/null || exit 0
trap 'rmdir "$lock_dir"' EXIT INT TERM
while tmux has-session >/dev/null 2>&1; do
  refresh
  sleep "$interval"
done
