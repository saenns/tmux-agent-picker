#!/usr/bin/env bash
set -u

target=${1:?missing SSH target}
remote_command=${2:?missing remote command}
control_path="$HOME/.ssh/tmux-agent-picker-bridge-%C"
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
