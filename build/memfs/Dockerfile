FROM python:3.12-slim

ARG MEMFS_UID=1000
ARG MEMFS_GID=1000

RUN apt-get update \
 && apt-get install -y --no-install-recommends git tini gosu \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid ${MEMFS_GID} memfs \
 && useradd --uid ${MEMFS_UID} --gid ${MEMFS_GID} --shell /usr/sbin/nologin --no-create-home memfs \
 && mkdir -p /data \
 && chown ${MEMFS_UID}:${MEMFS_GID} /data

COPY git-memfs-server.py /usr/local/bin/git-memfs-server.py
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0555 /usr/local/bin/git-memfs-server.py /usr/local/bin/entrypoint.sh

ENV MEMFS_BASE=/data \
    MEMFS_PORT=8285 \
    MEMFS_BIND=0.0.0.0 \
    MEMFS_UID=${MEMFS_UID} \
    MEMFS_GID=${MEMFS_GID}

VOLUME ["/data"]
EXPOSE 8285

ENTRYPOINT ["tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["python3", "/usr/local/bin/git-memfs-server.py"]
