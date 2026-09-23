#!/bin/sh
# SPDX-License-Identifier: Apache-2.0

set -eu

test -n "${CI_AUTHORIZED_KEY:-}"
printf '%s\n' "$CI_AUTHORIZED_KEY" > /home/ci/.ssh/authorized_keys
chown ci:ci /home/ci/.ssh/authorized_keys
chmod 0600 /home/ci/.ssh/authorized_keys
unset CI_AUTHORIZED_KEY
ssh-keygen -A
exec /usr/sbin/sshd -D -e
