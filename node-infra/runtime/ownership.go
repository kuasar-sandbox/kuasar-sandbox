// SPDX-License-Identifier: Apache-2.0
package main

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/vishvananda/netlink"
	"github.com/vishvananda/netns"
	"golang.org/x/sys/unix"
)

const inputsRoot = "/run/node-infra/netns-inputs"

func ensureNamespaceMount() error {
	data, err := os.ReadFile("/proc/self/mountinfo")
	if err != nil {
		return err
	}
	for _, line := range strings.Split(string(data), "\n") {
		fields := strings.Fields(line)
		if len(fields) > 4 && fields[4] == namespaceRoot {
			return nil // Reuse the runner's retained bind and its propagation.
		}
	}
	// ip netns add self-binds this directory on first use. Publish external
	// handles only after that parent mount exists: binding it later can leave
	// a second, hidden external mount beneath the published pin, making its
	// final unlink fail with EBUSY. This bind is private to the container.
	return unix.Mount(namespaceRoot, namespaceRoot, "", unix.MS_BIND, "")
}

func namespaceIdentity(path string) (uint64, uint64, error) {
	f, err := os.Open(path)
	if err != nil {
		return 0, 0, err
	}
	defer f.Close()
	var fs unix.Statfs_t
	if err := unix.Fstatfs(int(f.Fd()), &fs); err != nil {
		return 0, 0, err
	}
	if fs.Type != unix.NSFS_MAGIC {
		return 0, 0, fmt.Errorf("%s is not an nsfs handle", path)
	}
	kind, err := unix.IoctlRetInt(int(f.Fd()), unix.NS_GET_NSTYPE)
	if err != nil || kind != unix.CLONE_NEWNET {
		return 0, 0, fmt.Errorf("%s is not a network namespace", path)
	}
	var st unix.Stat_t
	if err := unix.Fstat(int(f.Fd()), &st); err != nil {
		return 0, 0, err
	}
	return st.Dev, st.Ino, nil
}

// Only native referenced names are considered. A supplied placeholder, symlink,
// wrong namespace type or directory fails; it is never replaced by a new netns.
func externalInputs(r *record) error {
	for i := range r.Namespaces {
		ns := &r.Namespaces[i]
		path := filepath.Join(inputsRoot, ns.Name)
		info, err := os.Lstat(path)
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("linked namespace input %s", path)
		}
		if ns.Name == r.SwitchNetNS || ns.Name == r.PortNetNS {
			return errors.New("external switch/port namespace is not supported")
		}
		ns.InputDevice, ns.InputInode, err = namespaceIdentity(path)
		if err != nil {
			return err
		}
		ns.External = true
	}
	return nil
}

func publishNamespace(ns namespace) error {
	if !ns.External {
		return execute("ip", "netns", "add", ns.Name)
	}
	source := filepath.Join(inputsRoot, ns.Name)
	dev, ino, err := namespaceIdentity(source)
	if err != nil {
		return err
	}
	if dev != ns.InputDevice || ino != ns.InputInode {
		return fmt.Errorf("external input %s changed", ns.Name)
	}
	f, err := os.Open(source)
	if err != nil {
		return err
	}
	defer f.Close()
	var st unix.Stat_t
	if err := unix.Fstat(int(f.Fd()), &st); err != nil {
		return err
	}
	if st.Dev != dev || st.Ino != ino {
		return errors.New("external input changed while opening")
	}
	target := filepath.Join(namespaceRoot, ns.Name)
	t, err := os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_RDWR, 0600)
	if err != nil {
		return err
	}
	t.Close()
	return unix.Mount(fmt.Sprintf("/proc/self/fd/%d", f.Fd()), target, "", unix.MS_BIND, "")
}

func initializeOwnership(r *record) error {
	boot, err := os.ReadFile("/proc/sys/kernel/random/boot_id")
	if err != nil {
		return err
	}
	id := make([]byte, 16)
	if _, err := rand.Read(id); err != nil {
		return err
	}
	r.BootID, r.Generation = strings.TrimSpace(string(boot)), hex.EncodeToString(id)
	r.ConsumerUnits, err = consumerTemplates()
	if err != nil {
		return err
	}
	return nil
}

func publishOwnership(r *record) error {
	path := filepath.Join(namespaceRoot, ".node-infra-owner")
	if _, err := os.Lstat(path); !errors.Is(err, os.ErrNotExist) {
		return errors.New("namespace owner already exists")
	}
	pending := path + "-" + r.Generation
	f, err := os.OpenFile(pending, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	defer f.Close()
	if _, err := f.WriteString(r.BootID + ":" + r.Generation + "\n"); err != nil {
		return err
	}
	if err := f.Sync(); err != nil {
		return err
	}
	return os.Rename(pending, path)
}

func resourcesAbsent(r *record) bool {
	paths := []string{filepath.Join(bpfRoot, r.SwitchName)}
	for _, ns := range r.Namespaces {
		paths = append(paths, filepath.Join(namespaceRoot, ns.Name))
	}
	for _, path := range paths {
		if _, err := os.Lstat(path); !errors.Is(err, os.ErrNotExist) {
			return false
		}
	}
	return true
}

func validateOwnership(r *record, recovery bool) error {
	if r.Schema == 1 {
		return nil
	} // Old pinned tools/records retain their contract.
	boot, err := os.ReadFile("/proc/sys/kernel/random/boot_id")
	if err != nil {
		return err
	}
	if r.BootID == "" || r.Generation == "" || strings.TrimSpace(string(boot)) != r.BootID {
		return errors.New("recovery boot/generation differs; retain old records")
	}
	owner, err := os.ReadFile(filepath.Join(namespaceRoot, ".node-infra-owner"))
	if recovery && errors.Is(err, os.ErrNotExist) && (r.State == "preparing" || r.State == "releasing") && resourcesAbsent(r) {
		return nil
	}
	if err != nil {
		return err
	}
	if string(owner) != r.BootID+":"+r.Generation+"\n" {
		return errors.New("namespace directory belongs to another generation")
	}
	for _, ns := range r.Namespaces {
		if !ns.External {
			continue
		}
		// Recovery can use the retained reference after the caller removed its
		// input binding. If an input is present, replacement must fail closed.
		path := filepath.Join(inputsRoot, ns.Name)
		if _, err := os.Lstat(path); recovery && errors.Is(err, os.ErrNotExist) {
			continue
		}
		dev, ino, err := namespaceIdentity(path)
		if err != nil {
			return err
		}
		if dev != ns.InputDevice || ino != ns.InputInode {
			return fmt.Errorf("external input %s was replaced", ns.Name)
		}
	}
	return nil
}

// Native Stop discovers management links from populated slots. Cover rollback
// before slots exist by removing only switch-side veths in our owned namespace.
func removeManagementLinks(r *record) error {
	ns, err := netns.GetFromName(r.SwitchNetNS)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	defer ns.Close()
	h, err := netlink.NewHandleAt(ns)
	if err != nil {
		return err
	}
	defer h.Close()
	for _, m := range r.Management {
		if m.SwitchDevice == "" {
			continue
		}
		link, err := h.LinkByName(m.SwitchDevice)
		var missing netlink.LinkNotFoundError
		if errors.As(err, &missing) {
			continue
		}
		if err != nil {
			return err
		}
		if err := verifyManagementPeer(r, m); err != nil {
			return err
		}
		if err := h.LinkDel(link); err != nil {
			return err
		}
	}
	return nil
}

func planeSysctl(name string, args []string) error {
	script := []string{"sh", "-ec", "mount --bind /proc/sys/net /proc/sys/net; mount -o remount,bind,rw /proc/sys/net; exec sysctl \"$@\"", "sysctl"}
	if name == "" {
		return execute("unshare", append(append([]string{"--mount", "--propagation", "private"}, script...), args...)...)
	}
	return execute("ip", append(append([]string{"netns", "exec", name}, script...), args...)...)
}
