# Derived from Moby v26.1.3 profiles/apparmor/template.go (Apache-2.0).
# https://github.com/moby/moby/blob/v26.1.3/profiles/apparmor/template.go
# See LICENSE.apparmor. Only the host launcher loads this unique profile.
# No change_profile rule: nested daemons and applications inherit confinement.
#include <tunables/global>
profile @PROFILE@ flags=(attach_disconnected,mediate_deleted) {
  #include <abstractions/base>
  network,
  capability,
  file,
  umount,
  signal (receive) peer=unconfined,
  signal (receive) peer=runc,
  signal (receive) peer=crun,
  signal (receive) peer=/usr/bin/dockerd,
  signal (send,receive) peer=@PROFILE@,
  ptrace (trace,read,tracedby,readby) peer=@PROFILE@,

  # Moby's proc restrictions, with only private network sysctls writable.
  audit deny /proc/* w,
  audit deny /proc/{[^1-9],[^1-9][^0-9],[^1-9s][^0-9y][^0-9s],[^1-9][^0-9][^0-9][^0-9/]*}/** w,
  audit deny /proc/sys/[^kn]** w,
  audit deny /proc/sys/n[^e]** w,
  audit deny /proc/sys/ne[^t]** w,
  audit deny /proc/sys/net?** w,
  audit deny /proc/sys/kernel/{?,??,[^s][^h][^m]**} w,
  audit deny /proc/sysrq-trigger rwklx,
  audit deny /sysrq-trigger rwklx,
  audit deny /proc/kcore rwklx,
  audit deny /kcore rwklx,

  # Retain sysfs/firmware/securityfs restrictions; bpffs and the namespaced
  # cgroup subtree are the only writable sysfs trees. No profile management.
  # runc's detached proc mount reports /proc/sys/net paths as /sys/net.
  # This alias also refers only to the caller's private network namespace;
  # actual sysfs has no /sys/net tree. Keep every other sysfs tree restricted.
  audit deny /sys/[^fn]*/** wklx,
  audit deny /sys/n[^e]*/** wklx,
  audit deny /sys/ne[^t]*/** wklx,
  audit deny /sys/net?*/** wklx,
  audit deny /sys/f[^s]*/** wklx,
  audit deny /sys/fs/[^bc]*/** wklx,
  audit deny /sys/fs/b[^p]*/** wklx,
  audit deny /sys/fs/bp[^f]*/** wklx,
  audit deny /sys/fs/bpf?*/** wklx,
  audit deny /sys/fs/c[^g]*/** wklx,
  audit deny /sys/fs/cg[^r]*/** wklx,
  audit deny /sys/firmware/** rwklx,
  audit deny /sys/devices/virtual/powercap/** rwklx,
  audit deny /sys/kernel/security/** rwklx,

  # The outer Docker mount/cgroup namespaces exist before this profile starts.
  remount /sys/fs/cgroup/,
  mount options=(rw,bind) /proc/sys/net/ -> /proc/sys/net/,
  remount /proc/sys/net/,
  mount fstype=bpf -> /sys/fs/bpf/,
  # Hide only inner dockerd's detection input. Host enforcement stays enabled;
  # descendants cannot load profiles or escape this profile through exec.
  mount options=(rw,bind) /dev/null -> /sys/module/apparmor/parameters/enabled,
  remount /sys/module/apparmor/parameters/enabled,

  # Observed systemd-generator hardening: read-only bind remounts only,
  # including the mount roots (subtree rules do not cover the root itself).
  remount options in (ro,bind,nosuid,nodev,noexec,relatime) /{var/log/journal,dev,build,work,work/home,output,inputs/release,var/lib/containerd,var/lib/docker}/,
  remount options in (ro,bind,nosuid,nodev,noexec,relatime) /dev/{mqueue,pts}/,
  remount options=(ro,bind) /etc/{hosts,hostname,resolv.conf,machine-id},
  remount options=(ro,bind) /,
  mount options=(rw,rslave) -> /dev/,
  mount options=(rw,rbind) / -> /run/systemd/mount-rootfs/,
  mount fstype=tmpfs -> /dev/shm/,
  mount fstype=proc -> /run/systemd/namespace-*/,
  mount options=(rw,move) /run/systemd/namespace-*/dev/ -> /run/systemd/mount-rootfs/dev/,
  pivot_root /run/systemd/mount-rootfs/,
  mount options=(rw,shared) -> /var/lib/docker/,

  # systemd private unit mounts and nested Docker's own roots. No blanket
  # mount grant, host cgroup bind, block-device filesystem or securityfs mount.
  mount fstype=(tmpfs,proc,sysfs,devpts,mqueue,cgroup2,overlay) -> /var/lib/docker/**,
  mount fstype=(tmpfs,proc,sysfs,devpts,mqueue,cgroup2,overlay) -> /var/lib/containerd/**,
  # systemd generators mount a private tmpfs at /tmp itself before journald.
  mount fstype=tmpfs -> /tmp/,
  mount fstype=tmpfs -> /{run,tmp}/**,
  mount options in (rw,ro,bind,rbind) /** -> /var/lib/docker/**,
  mount options in (rw,ro,bind,rbind) /** -> /var/lib/containerd/**,
  mount options in (rw,ro,bind,rbind) /** -> /run/systemd/**,
  remount /var/lib/docker/**,
  remount /var/lib/containerd/**,
  remount /run/systemd/**,
  mount options in (rw,private,rprivate,slave,rslave) -> /,
  mount options in (rw,private,rprivate,slave,rslave) -> /var/lib/docker/**,
  mount options in (rw,private,rprivate,slave,rslave) -> /var/lib/containerd/**,
  # Docker's archive unpacker pivots into its private storage first, then
  # makes the detached old root private at this generated mountpoint.
  mount options=(rw,rprivate) -> /.pivot_root[0-9]*/,
  # ip netns uses this private /run subtree for namespace handles.
  mount options=(rw,rshared) -> /run/netns/,
  mount options=(rw,rbind) /run/netns/ -> /run/netns/,
  # AppArmor reports the nsfs handle source as its filesystem root.
  mount options=(rw,bind) / -> /run/netns/*,
  # ip netns switches sysfs to the selected network namespace. Existing sysfs
  # write/securityfs denials still apply to the newly mounted filesystem.
  mount fstype=sysfs -> /sys/,
  pivot_root /var/lib/docker/**,
  pivot_root /var/lib/containerd/**,
}
