#!/bin/sh
set -eu

# When started as root, normalize ownership of the data volume and drop privileges.
# When the container is started with `user: <uid>:<gid>` in compose, this branch
# is skipped and we trust the caller to have set up volume permissions.
if [ "$(id -u)" = "0" ]; then
    chown -R "${MEMFS_UID}:${MEMFS_GID}" /data
    exec gosu "${MEMFS_UID}:${MEMFS_GID}" "$@"
fi

exec "$@"
