# MemFS sidecar for self-hosted Letta

This image runs the [`git-memfs-server.py`][upstream] script from
Corykidios/local_letta_memfs_magic as its own container, alongside the
Letta server. It serves bare git repos for Letta's MemFS proxy at
`/v1/git/`, replacing what Letta Cloud's git server normally provides.

[upstream]: https://github.com/Corykidios/local_letta_memfs_magic

## Why a sidecar

The same script can be run inside the main Letta image as a background
process, but doing so loses Docker's per-process restart, signal
handling, and log multiplexing. A sidecar keeps each process supervised,
mirrors the rest of a typical compose stack (Postgres, Valkey, etc.),
and lets the main Letta image stay close to upstream.

## What's vendored

The script lives at `git-memfs-server.py` and is pinned to upstream
commit `3175e1fa1447cb345223a69c39b2f6dc351ceee8`. Patches versus
upstream are documented at the top of the file:

- env-driven `MEMFS_PORT`, `MEMFS_BIND`, `MEMFS_BASE`, `MEMFS_DEFAULT_ORG`
- default bind address changed from `127.0.0.1` to `0.0.0.0`
- proper SIGTERM handler so `docker stop` shuts the server down without
  relying on the grace-period SIGKILL

## Build

From the repo root:

```bash
docker build -t ganter/letta-memfs:0.1.0 build/memfs
```

The image runs as UID `1000` / GID `1000` by default. Override at build
time with `--build-arg MEMFS_UID=... --build-arg MEMFS_GID=...`.

## Compose snippet

Drop into your prod compose alongside `letta-api` and `redis`/`valkey`,
then remove the old `git:` service:

```yaml
services:
  letta-api:
    # In letta.env: LETTA_MEMFS_SERVICE_URL=http://memfs:8285
    # The letta-memfs volume is no longer mounted here — only memfs needs it.
    networks:
      letta_backend_net:
        ipv4_address: 172.20.0.98
    # … rest of your existing letta-api service unchanged …

  memfs:
    image: ganter/letta-memfs:0.1.0
    build:
      context: ./build/memfs
    container_name: letta-memfs
    hostname: memfs
    restart: unless-stopped
    cap_drop:
      - ALL
    cap_add:
      - CHOWN          # entrypoint chowns /data on volume init
      - SETUID         # gosu drops to MEMFS_UID
      - SETGID         # gosu drops to MEMFS_GID
      - DAC_OVERRIDE   # traverse pre-existing files owned by other UIDs during chown
      - FOWNER         # adjust permissions on files we don't own yet
    networks:
      letta_backend_net:
        ipv4_address: 172.20.0.100
    volumes:
      - letta-memfs:/data
    healthcheck:
      test: ["CMD-SHELL", "python3 -c 'import socket; socket.create_connection((\"127.0.0.1\",8285),2).close()'"]
      interval: 10s
      timeout: 3s
      retries: 3

volumes:
  letta-memfs:
```

The entrypoint chowns `/data` to `1000:1000` on first start and drops
privileges with `gosu`. To skip that and run unprivileged from the start,
add `user: "1000:1000"` to the service — but only after the volume's
existing files are already owned by 1000:1000.

## Migration from the in-container script

If you're moving from the previous "ADD git-server.py + spin it up in
startup.sh" approach, the data on disk was written as root. The new
sidecar runs as UID 1000, so you need a one-shot ownership fix on the
host before the new container starts:

```bash
docker run --rm -v letta-memfs:/data alpine \
    chown -R 1000:1000 /data
```

Also remove the in-container memfs lines from `letta/server/startup.sh`
(the `exec ./git-server.py … &` line) and the `ADD …git-server.py` and
`git` apt-package lines from the main `Dockerfile`. The current branch
already contains both of those reverts.

## Configuration reference

| Env var               | Default                 | Notes                                          |
|-----------------------|-------------------------|------------------------------------------------|
| `MEMFS_PORT`          | `8285`                  | TCP port the HTTP server listens on            |
| `MEMFS_BIND`          | `0.0.0.0`               | Bind address; keep `0.0.0.0` for sidecar use   |
| `MEMFS_BASE`          | `/data`                 | Root for `<org>/<agent>/repo.git/` layout      |
| `MEMFS_DEFAULT_ORG`   | `default-org`           | Used when no `X-Organization-Id` header sent   |
| `MEMFS_UID` / `_GID`  | `1000` / `1000`         | Read by `entrypoint.sh` for the chown + gosu   |
