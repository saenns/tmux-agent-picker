#!/usr/bin/env bash
set -u

plugin_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
sort_mode=$(tmux show-option -gqv @agent-picker-sort)
hosts=$(tmux show-option -gqv @agent-picker-hosts)
timeout=$(tmux show-option -gqv @agent-picker-ssh-timeout)
fzf_bin=$(tmux show-option -gqv @agent-picker-fzf)
current_window=$(tmux display-message -p '#{window_id}')

sort_mode=${sort_mode:-mru}
timeout=${timeout:-2}
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

while true; do
  rows=$("$plugin_dir/scripts/inventory.py" \
    --sort "$sort_mode" \
    --hosts "$hosts" \
    --timeout "$timeout" \
    --current-window "$current_window")

  selection=$(printf '%s\n' "$rows" | "$fzf_bin" \
    --ansi \
    --no-sort \
    --delimiter=$'\t' \
    --with-nth=2.. \
    --layout=reverse \
    --border=none \
    --info=inline \
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
      continue
      ;;
  esac

  # With --expect, the first line is empty for Enter and the selected row is second.
  [[ -n $row ]] || row=$action
  token=${row%%$'\t'*}
  [[ -n $token ]] && exec "$plugin_dir/scripts/inventory.py" --select "$token"
done
