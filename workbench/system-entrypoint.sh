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

# System mode is administrator-privileged. Keep host-global kernel settings and
# device management out of ordinary service startup. All mounts below stay in
# this container's private mount namespace.
mount -o remount,bind,ro /sys
mount --bind /proc/sys /proc/sys
mount -o remount,bind,ro /proc/sys
# Inner Docker must not load host-global profiles. This is only a private
# detection mask; no host policy is read, added, replaced or removed.
if [ -e /sys/module/apparmor/parameters/enabled ]; then
    mount --bind /dev/null /sys/module/apparmor/parameters/enabled
    mount -o remount,bind,ro /sys/module/apparmor/parameters/enabled
fi

# Docker exposes only this container's cgroup namespace root. Do not bind the
# host cgroup tree. systemd moves PID 1 to init.scope before delegating children.
mount -o remount,bind,rw /sys/fs/cgroup
# Only network-namespace sysctls become writable. Keep the other sysctl mounts.
mount --bind /proc/sys/net /proc/sys/net
mount -o remount,bind,rw /proc/sys/net
if ! mount -t bpf bpf /sys/fs/bpf; then
    echo 'workbench: private bpffs unavailable; BPF-dependent tasks cannot run' >&2
fi
# The launcher supplies this instance's ID; the image only has an empty file.
systemd-machine-id-setup
exec /sbin/init
