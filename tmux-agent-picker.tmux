#!/usr/bin/env bash

plugin_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

option() {
  local name=$1 default=$2 value
  value=$(tmux show-option -gqv "$name")
  printf '%s' "${value:-$default}"
}

key=$(option @agent-picker-key W)
width=$(option @agent-picker-width '92%')
height=$(option @agent-picker-height '92%')

tmux set-option -gq @agent-picker-sort "$(option @agent-picker-sort mru)"
tmux set-option -gq @agent-picker-ssh-timeout "$(option @agent-picker-ssh-timeout 2)"
tmux set-option -gq @agent-picker-summary-timeout "$(option @agent-picker-summary-timeout 20)"

tmux bind-key "$key" display-popup -E -w "$width" -h "$height" -T 'windows and agents' "$plugin_dir/scripts/picker.sh"

record="run-shell -b '$plugin_dir/scripts/record_mru.py \"#{window_id}\" \"#{@agent_picker_remote_key}\" \"#{session_id}\"'"
tmux set-hook -g 'after-new-window[7811]' "$record"
tmux set-hook -g 'after-select-window[7811]' "$record"
tmux set-hook -g 'session-window-changed[7811]' "$record"
tmux set-hook -g 'after-select-pane[7811]' "$record"
tmux run-shell -b "$plugin_dir/scripts/record_mru.py '#{window_id}' '#{@agent_picker_remote_key}' '#{session_id}'"
