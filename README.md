# d9s

A [k9s](https://k9scli.io/)-inspired TUI for Docker and Podman, built with [Textual](https://textual.textualize.io/) and the [Docker SDK for Python](https://docker-py.readthedocs.io/).

> **Note:** This tool was built almost entirely using [Claude Code](https://claude.ai/claude-code) (Anthropic's AI coding assistant, max mode). The human provided direction, tested the UI, and reported bugs — Claude wrote the code.

## Runtime support

d9s auto-detects both Docker and Podman. If both are running, resources from both runtimes are shown side by side — the **RT** column in every table shows `docker` (blue) or `podman` (magenta) for each resource.

Detection order:
1. `DOCKER_HOST` environment variable (if set)
2. Docker daemon (default socket)
3. Podman socket (`/run/user/$UID/podman/podman.sock`, `/run/podman/podman.sock`)

No configuration needed — if a runtime is available, d9s finds it automatically.

Operations (shell, logs, delete, compose up/down, prune) are routed to the correct runtime for each resource. System prune runs on all active runtimes (without `sudo` for Podman).

## Install

```bash
uv tool install .   # installs d9s globally in an isolated env
d9s
```

To uninstall:
```bash
uv tool uninstall d9s
```

Alternative:
```bash
pipx install .         # same concept, without uv
pip install -e .       # editable install inside an active venv
```

## Views

A navigation bar at the top of every screen shows all views. Press `1`–`5` to switch directly between them from anywhere — no need to go back to the main screen first.

The header shows the active runtimes (e.g. `Context: docker + podman`) and engine versions (e.g. `Engine: Docker 29.4.0 + Podman 4.9.3`).

### 1 — Containers (default)

The main view. Shows all containers from all runtimes with RT, ID, name, image, state, ports, age, and compose project path (read from the `com.docker.compose.project.working_dir` label — shows `-` for non-compose containers).

- **Workspace cycling** (`a`): toggle between **running** (default), **all**, and **exited** containers. Shown in the crumb bar.
- **Auto-refresh** every 5 s with in-place table diffs that preserve cursor position. Toggle with `A` (shows `[pause]` when off).
- **Filter** (`/`): type to filter by container name. Enter to apply, Esc to clear.
- **Command mode** (`:`): type commands with tab autocompletion.

### 2 — Images

Images from all runtimes with RT, ID, repository, tag, size, and creation date. Inspect details (tags, digests, architecture, layers, labels), delete individual images, or prune all dangling ones.

### 3 — Volumes

Volumes from all runtimes with RT, name, driver, and mountpoint. Inspect details (driver, mountpoint, options, labels), delete individual volumes, or prune all unused ones.

### 4 — Networks

Networks from all runtimes with RT, ID, name, driver, scope, subnet, and connected container count. Inspect details (IPAM config, connected containers, labels), delete individual networks, or prune unused ones.

### 5 — Compose

Lists all Compose projects detected from container labels across all runtimes. Shows RT, project name, working directory, service count, and running/stopped counts.

Inspect shows the full compose config per service: image/build context, ports, volumes, environment variables, dependencies, restart policy, and networks.

If you do a compose down, containers are removed and the project disappears from this view. Use `:up` / `:upd` / `:upp` or `W` from the Containers view to bring them back.

### Describe (container inspect)

Detailed view for a single container: name, ID, image, state, PID, restart policy, resource limits, command, entrypoint, ports, networks, mounts, labels, and environment variables.

Press `y` to toggle **raw JSON** — the full inspect output from the container runtime API.

### Inspect (images / volumes / networks / compose)

Detailed view for non-container resources. Shows resource-specific fields in a human-readable format. Press `y` to toggle raw JSON.

## Keybindings

### Global (all views)

| Key | Action |
|-----|--------|
| `1` | Go to Containers |
| `2` | Go to Images |
| `3` | Go to Volumes |
| `4` | Go to Networks |
| `5` | Go to Compose |
| `esc` / `q` | Back (from sub-views to Containers) |
| `ctrl+c` | Quit |

### Containers

| Key | Action |
|-----|--------|
| arrows | Navigate rows |
| `enter` / `d` | Describe container (inspect) |
| `l` | Stream logs (live, follows output) |
| `s` | Shell into container (exec -it) |
| `S` | Live stats (CPU, memory, network I/O, block I/O, PIDs) |
| `ctrl+d` | Remove container — rm -f (`x` toggles volume removal in confirm dialog) |
| `ctrl+k` | Kill container (SIGKILL) |
| `w` | Compose down (`x` toggles `-v` for volume removal) |
| `W` | Compose up (restart after down) |
| `P` | System prune — prune --all on all runtimes (prompts for password, `x` toggles `--force`) |
| `t` | Stop container (SIGTERM) |
| `u` | Start stopped container |
| `r` | Restart container |
| `R` | Refresh |
| `a` | Cycle workspace: running / all / exited |
| `A` | Toggle auto-refresh |
| `/` | Filter by name |
| `:` | Command mode |
| `?` | Help screen (`/` to search, vim-style) |

### Command mode

Press `:` to open. Tab cycles through matching commands and path completions.

| Command | Action |
|---------|--------|
| `:img` / `:images` | Switch to Images |
| `:vol` / `:volumes` | Switch to Volumes |
| `:net` / `:networks` | Switch to Networks |
| `:com` / `:compose` | Switch to Compose |
| `:up` | Fuzzy file finder — scans for compose files, type to filter, Enter to select and compose up -d |
| `:up <path>` | Compose up from a specific file or directory (auto-detect runtime, tab path completion) |
| `:upd` | Same as `:up` but forces Docker as the runtime |
| `:upd <path>` | Same as `:up <path>` but forces Docker |
| `:upp` | Same as `:up` but forces Podman as the runtime |
| `:upp <path>` | Same as `:up <path>` but forces Podman |
| `:prune` | System prune (same as `P`) |
| `:q!` | Force quit |

### Images / Volumes / Networks

| Key | Action |
|-----|--------|
| `enter` / `i` | Inspect selected resource |
| `d` | Delete selected |
| `p` | Prune unused / dangling |
| `R` | Refresh |

### Compose

| Key | Action |
|-----|--------|
| `enter` / `i` | Inspect compose config |
| `u` | compose up -d |
| `w` | compose down |
| `r` | compose restart |
| `R` | Refresh |
| `l` | Logs (first running service) |

### Describe / Inspect

| Key | Action |
|-----|--------|
| `y` | Toggle human-readable view / raw JSON |
| `esc` / `q` | Back |

## Requirements

- Python 3.10+
- Docker daemon and/or Podman socket active
- `textual >= 8.0`
- `docker >= 7.0`

Optional: `pyyaml` for parsed compose config inspection (falls back to raw output without it).
