#!/bin/bash
# Explicit first install on an empty Ubuntu host. Never run during recovery.
set -eu
umask 077
if [ "$(id -u)" -ne 0 ]; then
  echo 'Run the reviewed first-install script with sudo.' >&2
  exit 2
fi
for program in docker git flock install systemctl getent; do
  command -v "$program" >/dev/null
done
exec 9>/run/astral-paper-prepare.lock
flock -n 9
source_root=$(cd -- "$(dirname -- "$0")/../.." && pwd -P)
if [ -n "$(git -C "$source_root" status --porcelain --untracked-files=normal)" ]; then
  echo 'Checkout must match the reviewed commit before installation.' >&2
  exit 2
fi
# Never modify an existing install, mount, symlink or unrelated host UID.
for target in /var/lib/astral /etc/astral-paper /etc/systemd/system/astral-paper.service; do
  if [ -e "$target" ] || [ -L "$target" ]; then
    echo 'Existing installation detected; stop and reconcile, do not initialize.' >&2
    exit 2
  fi
done
if getent passwd 10001 >/dev/null || getent group 10001 >/dev/null; then
  echo 'Container UID/GID already allocated; review host ownership before installation.' >&2
  exit 2
fi
docker info >/dev/null
if docker image inspect astral-paper:reviewed >/dev/null 2>&1; then
  echo 'Reviewed image already exists; inspect prior installation before proceeding.' >&2
  exit 2
fi
docker build --pull -f "$source_root/deploy/ncloud/Dockerfile" -t astral-paper:reviewed "$source_root"
install -d -m 0700 -o 10001 -g 10001 /var/lib/astral
docker run --rm --pull=never --network=none --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges:true --user=10001:10001 \
  --mount=type=bind,src=/var/lib/astral,dst=/var/lib/astral \
  astral-paper:reviewed python -m astral_stock_agent.host_storage initialize-empty --directory /var/lib/astral
install -d -m 0700 /etc/astral-paper
install -m 0600 "$source_root/deploy/ncloud/paper.env.example" /etc/astral-paper/paper.env
install -m 0644 "$source_root/deploy/ncloud/astral-paper.service" /etc/systemd/system/astral-paper.service
systemctl daemon-reload
echo 'Prepared only. Service is not started or enabled; candidate and reporting remain disabled.'
