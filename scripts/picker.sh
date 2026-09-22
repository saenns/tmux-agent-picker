#!/usr/bin/env bash
set -u

plugin_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
sort_mode=$(tmux show-option -gqv @agent-picker-sort)
hosts=$(tmux show-option -gqv @agent-picker-hosts)
timeout=$(tmux show-option -gqv @agent-picker-ssh-timeout)
batch_mode=$(tmux show-option -gqv @agent-picker-ssh-batch-mode)
remote_tmux=$(tmux show-option -gqv @agent-picker-remote-tmux)
retries=$(tmux show-option -gqv @agent-picker-ssh-retries)
fzf_bin=$(tmux show-option -gqv @agent-picker-fzf)
current_window=$(tmux display-message -p '#{window_id}')

sort_mode=${sort_mode:-mru}
timeout=${timeout:-2}
batch_mode=${batch_mode:-yes}
retries=${retries:-0}
cache_root=${XDG_CACHE_HOME:-$HOME/.cache}/tmux-agent-picker
cache_key=$(printf '%s' "$hosts|$timeout|$batch_mode|$remote_tmux|$retries" | shasum -a 256 | awk '{print $1}')
cache_file="$cache_root/inventory-$cache_key.json"
live_socket="$cache_root/fzf-$$.sock"
mkdir -p "$cache_root"
rm -f -- "$live_socket"
trap 'rm -f -- "$live_socket"' EXIT
if [[ -z $fzf_bin ]]; then
  fzf_bin=$(command -v fzf 2>/dev/null || true)
fi
if [[ -z $fzf_bin && -x $HOME/.local/bin/fzf ]]; then
  fzf_bin=$HOME/.local/bin/fzf
fi
fzf_bin=${fzf_bin/#\~/$HOME}
if [[ -z $fzf_bin ]]; then
  tmux display-message 'tmux-agent-picker: fzf was not found; set @agent-picker-fzf'
  exit 1
fi

inventory() {
  "$plugin_dir/scripts/inventory.py" \
    --sort "$sort_mode" \
    --hosts "$hosts" \
    --timeout "$timeout" \
    --ssh-batch-mode "$batch_mode" \
    --remote-tmux "$remote_tmux" \
    --ssh-retries "$retries" \
    --current-window "$current_window" \
    --cache-file "$cache_file" "$@"
}

# fzf's local control socket lets a completed background refresh replace the
# list without closing the popup or asking the user to press Ctrl-R.
reload_command() {
  printf '%q ' "$plugin_dir/scripts/inventory.py" \
    --sort "$sort_mode" \
    --hosts "$hosts" \
    --timeout "$timeout" \
    --ssh-batch-mode "$batch_mode" \
    --remote-tmux "$remote_tmux" \
    --ssh-retries "$retries" \
    --current-window "$current_window" \
    --cache-file "$cache_file"
}

refresh_in_background() {
  (
    inventory --refresh-cache >/dev/null 2>&1
    [[ -S $live_socket ]] || exit 0
    command -v curl >/dev/null 2>&1 || exit 0
    curl --silent --max-time 1 --unix-socket "$live_socket" \
      -X POST http://localhost/ -d "reload($(reload_command))" >/dev/null 2>&1 || true
  ) &
}

refresh=0
while true; do
  if [[ $refresh == 1 || ! -s $cache_file ]]; then
    rows=$(inventory --refresh-cache)
    refresh=0
  else
    rows=$(inventory)
    refresh_in_background
  fi

  selection=$(printf '%s\n' "$rows" | "$fzf_bin" \
    --ansi \
    --no-mouse \
    --no-sort \
    --delimiter=$'\t' \
    --with-nth=2.. \
    --layout=reverse \
    --border=none \
    --info=inline \
    --listen="$live_socket" \
    --prompt="windows [$sort_mode]> " \
    --header='enter: open   alt-s/ctrl-s: sort   ctrl-r: refresh   esc: close' \
    --expect=alt-s,ctrl-s,ctrl-r \
    --bind='ctrl-j:down,ctrl-k:up,ctrl-d:half-page-down,ctrl-u:half-page-up')
  status=$?

  if (( status != 0 )); then
    exit 0
  fi

  action=$(printf '%s\n' "$selection" | sed -n '1p')
  row=$(printf '%s\n' "$selection" | sed -n '2p')
  case "$action" in
    alt-s|ctrl-s)
      if [[ $sort_mode == mru ]]; then sort_mode=state; else sort_mode=mru; fi
      continue
      ;;
    ctrl-r)
      refresh=1
      continue
      ;;
  esac

  # With --expect, the first line is empty for Enter and the selected row is second.
  [[ -n $row ]] || row=$action
  token=${row%%$'\t'*}
  [[ -n $token ]] && exec "$plugin_dir/scripts/inventory.py" --remote-tmux "$remote_tmux" --ssh-retries "$retries" --select "$token"
done
