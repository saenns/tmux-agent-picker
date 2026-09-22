#!/usr/bin/env bash
set -u

target=${1:?missing SSH target}
remote_command=${2:?missing remote command}
# A single OpenSSH master has a limited number of simultaneous session
# channels.  Each picker selection is an interactive channel which can stay
# open in a tmux window, so sharing one master per host eventually causes
# "Session open refused by peer".  Keep a reusable master per remote tmux
# target instead.  Reopening that target remains fast, while many open remote
# windows no longer exhaust one master's channel limit.
bridge_key=$(printf '%s\\0%s' "$target" "$remote_command" | shasum -a 256 | /usr/bin/awk '{print substr($1, 1, 16)}')
# Keep this short: OpenSSH appends a temporary suffix while creating the
# socket, and macOS has a small Unix-domain socket pathname limit.
control_path="$HOME/.ssh/tap-${bridge_key}-%C"

# `ControlMaster=auto` does not remove a dead socket itself; it emits a
# warning and falls back to a new non-multiplexed connection. Clear only a
# socket that cannot answer the master's health check before opening a bridge.
resolved_control_path=$(ssh -G -o "ControlPath=$control_path" "$target" 2>/dev/null | /usr/bin/awk '$1 == "controlpath" { print $2; exit }')
if [[ -n "$resolved_control_path" && -S "$resolved_control_path" ]] && ! ssh -o "ControlPath=$control_path" -O check "$target" >/dev/null 2>&1; then
  rm -f -- "$resolved_control_path"
fi
ssh_args=(
  ssh -tt
  -o ControlMaster=auto
  -o ControlPersist=6h
  -o "ControlPath=$control_path"
  "$target" "$remote_command"
)

"${ssh_args[@]}" && exit 0

# An SSM/WSSH tunnel can disappear while its control socket remains. Remove
# that master and retry once with a fresh connection.
ssh -o "ControlPath=$control_path" -O exit "$target" >/dev/null 2>&1 || true
"${ssh_args[@]}"
