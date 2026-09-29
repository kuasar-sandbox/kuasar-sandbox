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
  remount options=(ro,bind) /{var/log/journal,dev,build,work,work/home,output,inputs/release,var/lib/containerd,var/lib/docker}/,
  remount options=(ro,bind) /dev/{mqueue,pts}/,
  remount options=(ro,bind) /etc/{hosts,hostname,resolv.conf,machine-id},
  remount options=(ro,bind) /,
  mount options=(rw,rslave) -> /dev/,
  mount options=(rw,rbind) / -> /run/systemd/mount-rootfs/,
  mount fstype=tmpfs -> /dev/shm/,
  mount fstype=proc -> /run/systemd/namespace-*/,
  # The namespace-cleanup case creates a child PID namespace with its own
  # proc mount. Existing proc write denials remain in force after the mount.
  mount fstype=proc -> /proc/,
  mount options=(rw,move) /run/systemd/namespace-*/dev/ -> /run/systemd/mount-rootfs/dev/,
  mount options=(rw,move) /run/systemd/namespace-*/ -> /run/systemd/mount-rootfs/proc/,
  pivot_root /run/systemd/mount-rootfs/,
  mount options=(rw,shared) -> /var/lib/docker/,

  # systemd private unit mounts and nested Docker's own roots. No blanket
  # mount grant, host cgroup bind, block-device filesystem or securityfs mount.
  mount fstype=(tmpfs,proc,sysfs,devpts,mqueue,cgroup2,overlay) -> /var/lib/docker/**,
  mount fstype=(tmpfs,proc,sysfs,devpts,mqueue,cgroup2,overlay) -> /var/lib/containerd/**,
  # systemd generators mount a private tmpfs at /tmp itself before journald.
  mount fstype=tmpfs -> /tmp/,
  mount fstype=tmpfs -> /{run,tmp}/**,
  # The canonical telemetry ENOSPC case mounts its bounded scratch filesystem.
  mount fstype=tmpfs -> /work/**/usage-faults/enospc-base/,
  mount options=(rw,bind) /** -> /var/lib/docker/**,
  mount options=(rw,rbind) /** -> /var/lib/docker/**,
  mount options=(ro,bind) /** -> /var/lib/docker/**,
  mount options=(ro,rbind) /** -> /var/lib/docker/**,
  mount options=(rw,bind) /** -> /var/lib/containerd/**,
  mount options=(rw,rbind) /** -> /var/lib/containerd/**,
  mount options=(ro,bind) /** -> /var/lib/containerd/**,
  mount options=(ro,rbind) /** -> /var/lib/containerd/**,
  mount options=(rw,bind) /** -> /run/systemd/**,
  mount options=(rw,rbind) /** -> /run/systemd/**,
  mount options=(ro,bind) /** -> /run/systemd/**,
  mount options=(ro,rbind) /** -> /run/systemd/**,
  remount /var/lib/docker/**,
  remount /var/lib/containerd/**,
  remount /run/systemd/**,
  mount options=(rw,private) -> /,
  mount options=(rw,rprivate) -> /,
  mount options=(rw,slave) -> /,
  mount options=(rw,rslave) -> /,
  mount options=(rw,private) -> /var/lib/docker/**,
  mount options=(rw,rprivate) -> /var/lib/docker/**,
  mount options=(rw,slave) -> /var/lib/docker/**,
  mount options=(rw,rslave) -> /var/lib/docker/**,
  mount options=(rw,private) -> /var/lib/containerd/**,
  mount options=(rw,rprivate) -> /var/lib/containerd/**,
  mount options=(rw,slave) -> /var/lib/containerd/**,
  mount options=(rw,rslave) -> /var/lib/containerd/**,
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
  # After pivot, runc hardens the inner proc mount with self-binds followed
  # only by read-only remounts. No alternate source or writable remount grant.
  mount options=(rw,rbind) /proc/asound/ -> /proc/asound/,
  mount options=(rw,rbind) /proc/bus/ -> /proc/bus/,
  mount options=(rw,rbind) /proc/fs/ -> /proc/fs/,
  mount options=(rw,rbind) /proc/irq/ -> /proc/irq/,
  mount options=(rw,rbind) /proc/sys/ -> /proc/sys/,
  mount options=(rw,rbind) /proc/sysrq-trigger -> /proc/sysrq-trigger,
  remount options=(ro,bind) /proc/{asound,bus,fs,irq,sys}/,
  remount options=(ro,bind,nosuid,nodev,noexec) /proc/{asound,bus,fs,irq,sys}/,
  remount options=(ro,bind,nosuid,nodev,noexec,relatime) /proc/{asound,bus,fs,irq,sys}/,
  remount options=(ro,bind) /proc/sysrq-trigger,
  remount options=(ro,bind,nosuid,nodev,noexec) /proc/sysrq-trigger,
  remount options=(ro,bind,nosuid,nodev,noexec,relatime) /proc/sysrq-trigger,
  # Standard OCI masked paths used by nested runc: directories become
  # read-only tmpfs and file targets become /dev/null binds. These exact
  # destinations cannot expose or make the underlying host proc/sysfs writable.
  mount fstype=tmpfs options=(ro) -> /proc/{acpi,scsi}/,
  mount fstype=tmpfs options=(ro) -> /sys/firmware/,
  mount fstype=tmpfs options=(ro) -> /sys/devices/virtual/powercap/,
  mount options=(rw,bind) /dev/null -> /proc/{interrupts,kcore,keys,latency_stats,sched_debug,timer_list,timer_stats},
  # Inner dockerd persists a private network namespace handle under its own
  # /run tree. The source is only a network-namespace fd exposed through proc.
  mount options=(rw,bind) / -> /run/docker/netns/*,
  pivot_root /var/lib/docker/**,
  pivot_root /var/lib/containerd/**,
}
