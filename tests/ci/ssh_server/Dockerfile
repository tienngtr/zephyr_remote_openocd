# SPDX-License-Identifier: Apache-2.0

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install --no-install-recommends --yes \
        openssh-server \
        openocd \
        passwd \
        python3 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash ci \
    && passwd --delete ci \
    && install -d -o ci -g ci -m 0700 /home/ci/.ssh \
    && install -d -m 0755 /run/sshd \
    && rm -f /etc/ssh/ssh_host_*

COPY sshd_config /etc/ssh/sshd_config
COPY entrypoint.sh /usr/local/sbin/ci-sshd-entrypoint

RUN chmod 0755 /usr/local/sbin/ci-sshd-entrypoint

EXPOSE 22

ENTRYPOINT ["/usr/local/sbin/ci-sshd-entrypoint"]
