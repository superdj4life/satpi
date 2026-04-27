# Project rules for Claude

This file is for Claude (Cowork / Claude Code) when working on the **satpi**
project. Read it at the start of every session and follow these rules.

## 1. Single source of truth: satpi5

All changes to the `satpi` project (code under `bin/`, `scripts/`,
`config/`, `docs/`, `systemd/`, etc.) are made on **satpi5 only**.

Workflow for any project change:

1. Edit the file in the Cowork output folder.
2. `scp` it to `satpi5:~/satpi/<relative-path>`.
3. From satpi5, run `git add` / `git commit` / `git push`.
4. GitHub is the single source of truth.

Never run `git add`, `git commit`, or `git push` on satpi4 or satpi6.

## 2. satpi4 and satpi6 are pull-only consumers

These hosts only receive code by `git pull` from GitHub, and only when the
user explicitly asks for it.

- Do NOT run `git pull` on satpi4 or satpi6 on your own initiative.
- Wait for the user to say something like "auf satpi4 jetzt git pull" or
  "pull on satpi6".

If those hosts have local uncommitted edits in their `~/satpi` working
tree, leave them alone — the user resolves them themselves on satpi5.

## 3. System operations are allowed on any host

These are NOT part of the "project" rule above and may be performed on
any of the three Pis as needed:

- `apt install`, `systemctl`, `journalctl`
- Building / installing system tools (e.g. SatDump under `/usr/local/src`)
- Editing system files like `/boot/firmware/config.txt`,
  `/etc/modprobe.d/...`, `/etc/systemd/system/...`
- Diagnostics (`dmesg`, `lsusb`, `vcgencmd`, etc.)

## 4. When unsure, ask

Before any destructive or git-mutating operation on a host other than
satpi5 — ask the user first.

## 5. Common pitfalls to avoid

- Pasting `ssh ...` into an already-SSHed terminal nests sessions; use
  the existing terminal.
- Long-running builds should run inside `tmux` so VPN drops don't kill
  them: `tmux new -s build` then run, detach with `Ctrl-B d`,
  reattach with `tmux attach -t build`.
- Cloning large repos through an unstable VPN: use shallow clone with
  the target tag, e.g. `git clone --depth 1 --branch 1.2.2 ...`.
