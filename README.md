# tmux-agent-picker

A popup-only tmux window picker for ordinary shells and coding-agent sessions.
It keeps tmux in charge of the terminal, adds Codex/Claude state metadata, sorts
by real window-focus MRU or urgency, and can include tmux servers reached over
SSH.

## Install

Clone it and source the plugin from `~/.tmux.conf`:

```sh
git clone https://github.com/saenns/tmux-agent-picker ~/.tmux/plugins/tmux-agent-picker
```

```tmux
set -g @agent-picker-key 'W'
run-shell ~/.tmux/plugins/tmux-agent-picker/tmux-agent-picker.tmux
```

Or install it with [TPM](https://github.com/tmux-plugins/tpm):

```tmux
set -g @plugin 'saenns/tmux-agent-picker'
```

Reload tmux, then use `prefix + W`. The picker is temporary and does not change
tmux copy mode or application keybindings.

Picker keys:

- `Enter`: open a window
- `Alt-s` or `Ctrl-s`: toggle MRU/state sorting
- `Ctrl-r`: refresh
- `Ctrl-j` / `Ctrl-k`, `Ctrl-d` / `Ctrl-u`: move using familiar vi-style controls
- `Esc`: close

## Agent state hooks

Install hooks without replacing other hook definitions:

```sh
scripts/install_hooks.py --codex
scripts/install_hooks.py --claude
```

Codex requires newly installed hooks to be reviewed and trusted with `/hooks`.
Run the installer on remote hosts as well if accurate remote agent state is
needed. Hosts without hooks still show shells, foreground commands, and directly
detectable agent executables.

States are `WAIT`, `DONE`, `WORK`, `IDLE`, `RUN`, `SH`, `DEAD`, and `OFF`.
`DONE` changes to `IDLE` when its window is viewed.

## One-line summaries

Rows show a cached one-line summary. Agent `Stop` hooks immediately derive a
local summary from `last_assistant_message`, so opening the picker never waits
for a network request. For panes without hook metadata, the picker uses the last
meaningful line from 80 lines of local scrollback. Scrollback is not sent to an
API.

Optionally, a detached worker can improve agent-turn summaries with a fast
OpenAI model:

```tmux
set -g @agent-picker-summary-model 'gpt-5.6-luna'
set -g @agent-picker-summary-timeout 20
```

The worker runs only when `OPENAI_API_KEY` is present in the agent hook's
environment. It sends only the latest assistant message, requests no response
storage, and replaces the local summary only if the result still belongs to the
latest turn. Leave the model option empty to keep summaries entirely local. Do
not put the API key in `.tmux.conf`.

Remote cached summaries appear when the same hooks/plugin are installed on the
remote host. Remote scrollback is never fetched.

## Remote hosts

Configure comma-separated `label=ssh-target` entries:

```tmux
set -g @agent-picker-hosts 'dev=me@devbox,gpu=gpu-box'
set -g @agent-picker-ssh-timeout 2
set -g @agent-picker-fzf '~/.local/bin/fzf' # only needed when fzf is outside tmux's PATH
```

Inventory uses non-interactive SSH, so keys or another non-interactive login
method must already work. Selecting a remote row creates a local bridge window
that runs `ssh -tt HOST tmux attach-session -t SESSION`; selecting it again
reuses that bridge. The remote tmux server supplies persistence.

MRU focus tracking needs no remote plugin: while collecting inventory, the
picker installs a small native tmux hook in the running remote server. It starts
tracking after the first inventory refresh and is reinstalled automatically if
that server restarts. Installing the plugin remotely remains useful for agent
state and cached summaries.

Because this is tmux inside tmux, the outer tmux consumes the first prefix. Send
the prefix twice to address the remote tmux, or configure a different prefix on
remote hosts.

## Options

```tmux
set -g @agent-picker-key 'W'
set -g @agent-picker-width '92%'
set -g @agent-picker-height '92%'
set -g @agent-picker-sort 'mru'       # or state
set -g @agent-picker-hosts ''
set -g @agent-picker-ssh-timeout 2
set -g @agent-picker-summary-model '' # optional; local-only when empty
set -g @agent-picker-summary-timeout 20
```

Requires tmux 3.2+, fzf, Python 3.10+, and SSH for remote hosts.
