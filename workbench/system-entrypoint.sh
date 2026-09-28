#!/bin/sh
set -eu

# An ordinary UID uses the same toolchain image without any system privileges.
if [ "${1:-}" = build ]; then
    shift
    [ "$#" -gt 0 ] || { echo 'workbench build needs a command' >&2; exit 2; }
    exec "$@"
fi
[ "$(id -u)" = 0 ] || { echo 'system mode requires container root; use build for ordinary UID work' >&2; exit 1; }
[ "$(cat /proc/self/cgroup)" = '0::/' ] || { echo 'a private cgroup namespace is required' >&2; exit 1; }
[ "$(stat -fc %T /sys/fs/cgroup)" = cgroup2fs ] || { echo 'cgroup v2 is required' >&2; exit 1; }

# This changes detection only in this private mount namespace. Host AppArmor
# stays enabled and all descendants inherit the enforced outer profile. Inner
# Docker must not inspect/load/change host-global docker-default profiles.
if [ -e /sys/module/apparmor/parameters/enabled ]; then
    mount --bind /dev/null /sys/module/apparmor/parameters/enabled
    mount -o remount,bind,ro /sys/module/apparmor/parameters/enabled
fi

# Docker exposes only this container's cgroup namespace root. Do not bind the
# host cgroup tree. systemd moves PID 1 to init.scope before delegating children.
mount -o remount,rw /sys/fs/cgroup
# Only network-namespace sysctls become writable. Keep the other sysctl mounts.
mount --bind /proc/sys/net /proc/sys/net
mount -o remount,bind,rw /proc/sys/net
mount -t bpf bpf /sys/fs/bpf
# The launcher supplies this instance's ID; the image only has an empty file.
systemd-machine-id-setup
exec /sbin/init
