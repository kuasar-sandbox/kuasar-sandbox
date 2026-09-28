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
  deny /proc/* w,
  deny /proc/{[^1-9],[^1-9][^0-9],[^1-9s][^0-9y][^0-9s],[^1-9][^0-9][^0-9][^0-9/]*}/** w,
  deny /proc/sys/[^kn]** w,
  deny /proc/sys/n[^e]** w,
  deny /proc/sys/ne[^t]** w,
  deny /proc/sys/net?** w,
  deny /proc/sys/kernel/{?,??,[^s][^h][^m]**} w,
  deny /proc/sysrq-trigger rwklx,
  deny /proc/kcore rwklx,

  # Retain sysfs/firmware/securityfs restrictions; bpffs and the namespaced
  # cgroup subtree are the only writable sysfs trees. No profile management.
  deny /sys/[^f]*/** wklx,
  deny /sys/f[^s]*/** wklx,
  deny /sys/fs/[^bc]*/** wklx,
  deny /sys/fs/b[^p]*/** wklx,
  deny /sys/fs/bp[^f]*/** wklx,
  deny /sys/fs/bpf?*/** wklx,
  deny /sys/fs/c[^g]*/** wklx,
  deny /sys/fs/cg[^r]*/** wklx,
  deny /sys/firmware/** rwklx,
  deny /sys/devices/virtual/powercap/** rwklx,
  deny /sys/kernel/security/** rwklx,

  # The outer Docker mount/cgroup namespaces exist before this profile starts.
  remount /sys/fs/cgroup/,
  mount options=(rw,bind) /proc/sys/net/ -> /proc/sys/net/,
  remount /proc/sys/net/,
  mount fstype=bpf -> /sys/fs/bpf/,
  # Hide only inner dockerd's detection input. Host enforcement stays enabled;
  # descendants cannot load profiles or escape this profile through exec.
  mount options=(rw,bind) /dev/null -> /sys/module/apparmor/parameters/enabled,
  remount /sys/module/apparmor/parameters/enabled,

  # systemd private unit mounts and nested Docker's own roots. No blanket
  # mount grant, host cgroup bind, block-device filesystem or securityfs mount.
  mount fstype=(tmpfs,proc,sysfs,devpts,mqueue,cgroup2,overlay) -> /var/lib/docker/**,
  mount fstype=(tmpfs,proc,sysfs,devpts,mqueue,cgroup2,overlay) -> /var/lib/containerd/**,
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
  pivot_root /var/lib/docker/**,
  pivot_root /var/lib/containerd/**,
}
