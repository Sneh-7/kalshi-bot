# Development environment setup

Notes on how this repository and its development machine are configured. Unlike
[`ARCHITECTURE.md`](ARCHITECTURE.md), everything here **describes work that has
actually been done**.

---

## Repository facts

| Property | Value |
| --- | --- |
| Remote | `https://github.com/Sneh-7/kalshi-bot` |
| Visibility | **Public** |
| Default branch | `main` |
| Protocol | HTTPS (not SSH) |
| Tracking | `main` → `origin/main` |

## Git identity

Configured **locally for this repository only** (not globally):

```bash
git config user.name  "Sneh Patel"
git config user.email "snehpatel40@gmail.com"
```

This matters for attribution: commits authored with an email GitHub doesn't
recognize show up as an unlinked name rather than your profile. The initial commit
was originally authored as `sneh@mac.mynetworksettings.com` — a hostname-derived
address Git invents when no identity is set — and was amended to fix this.

To make the identity apply to all repositories instead, add `--global`.

## GitHub authentication

Authentication uses the **GitHub CLI**, with the token stored in the macOS keyring.

### Installing `gh` without Homebrew

This machine has no Homebrew, so `gh` was installed from the official standalone
release tarball into a user-local directory — no `sudo` required:

```bash
VER=2.96.0
curl -fsSL -o /tmp/gh.zip \
  "https://github.com/cli/cli/releases/download/v${VER}/gh_${VER}_macOS_arm64.zip"
unzip -q /tmp/gh.zip -d /tmp/ghx
mkdir -p "$HOME/.local/bin"
cp "/tmp/ghx/gh_${VER}_macOS_arm64/bin/gh" "$HOME/.local/bin/gh"
chmod +x "$HOME/.local/bin/gh"
xattr -d com.apple.quarantine "$HOME/.local/bin/gh"   # clear Gatekeeper flag
```

The `xattr` step is easy to miss. macOS marks downloaded binaries with a quarantine
attribute; without clearing it, Gatekeeper blocks execution with a dialog rather
than a useful error.

Use the `arm64` build on Apple Silicon and `amd64` on Intel — check with `uname -m`.

### PATH

This machine had **no shell rc files at all**, so `~/.zshrc` was created containing:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Without this, `gh` is installed but unreachable — `command not found` in any new
Terminal. Note that an agent/IDE session may already have `~/.local/bin` on its PATH
even when your interactive shell does not; verify what your real shell sees with:

```bash
env -i HOME="$HOME" /bin/zsh -l -c 'echo $PATH'
```

### Logging in

```bash
gh auth login
```

Answers used: **GitHub.com** → **HTTPS** → **Yes** (authenticate Git with your
GitHub credentials) → **Login with a web browser**.

That third answer is the one that matters. Answering "No" authenticates the `gh`
tool but leaves Git itself without credentials, so `git push` still fails with:

```
fatal: could not read Username for 'https://github.com'
```

If that happens, fix it without re-running the whole login:

```bash
gh auth setup-git
```

Verify:

```bash
gh auth status
```

Current state: logged in as **Sneh-7**, token in keyring, scopes `gist`, `read:org`,
`repo`.

`gh auth login` needs an interactive terminal for its prompts and a browser for the
device flow. It does not run reliably from a non-interactive or automated shell.

## Ignored paths

`.gitignore` covers standard Python artifacts (`__pycache__/`, `*.py[cod]`,
`.venv/`, `env/`, `dist/`, `build/`, `*.egg-info/`, `.pytest_cache/`,
`.mypy_cache/`) and `.env`, plus two project-specific entries:

### `polymarket-sentiment-agent/`

A **separate upstream repository**
([`priyanshshahh/polymarket-sentiment-agent`](https://github.com/priyanshshahh/polymarket-sentiment-agent))
checked out inside this folder. It has its own `.git` and its own remote.

It was originally committed here as a **broken gitlink**: a mode-`160000` submodule
pointer with **no `.gitmodules` file**. Git recorded the commit SHA but not the URL,
so a fresh clone would produce an empty directory that `git submodule update --init`
could not populate — and none of the contents were backed up either. The entry was
removed with `git rm --cached` and the path added to `.gitignore`.

> ⚠️ **That directory contains substantial uncommitted local modifications** across
> its backend (`auth.py`, `routes.py`, `execution.py`, `intelligence.py`, and
> others). Because it is ignored here and unpushed there, **that work exists only on
> this machine and is not backed up anywhere.** Forking the upstream repo and
> pushing to your own remote would address this.

Do not `git add` this directory.

### `docs/SESSION_LOG.md`

A detailed local log of the development session that produced this setup. It is
**deliberately gitignored** because this repository is public and the log contains
personal and machine-specific details. It stays on the local machine only.

## Common operations

```bash
# Status
git status
git log --oneline

# Commit and push
git add -A
git commit -m "message"
git push

# Confirm what's actually on the remote
git ls-remote origin
gh api repos/Sneh-7/kalshi-bot/contents --jq '.[].name'

# Open the repo in a browser
gh repo view --web
```

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `gh: command not found` | `~/.local/bin` not on PATH | Add the export to `~/.zshrc`; open a new terminal |
| `could not read Username for 'https://github.com'` | Git has no credentials | `gh auth setup-git` |
| `remote origin already exists` | `git remote add` run on a configured repo | Harmless — verify with `git remote -v` |
| `Everything up-to-date` | Nothing new to push | Not an error |
| Gatekeeper blocks `gh` | Quarantine attribute | `xattr -d com.apple.quarantine ~/.local/bin/gh` |
| `timeout: command not found` | GNU coreutils absent on macOS | Use background jobs, or `brew install coreutils` (`gtimeout`) |

## Machine notes

- macOS 15.4.1, arm64 (Apple Silicon)
- **No Homebrew.** Install CLI tools from official tarballs into `~/.local/bin`.
- **No GNU `timeout`.** Scripts depending on it need an alternative.
- Shell is `/bin/zsh`; `~/.zshrc` was created during setup and previously did not exist.
