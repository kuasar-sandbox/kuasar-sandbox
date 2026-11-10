// SPDX-License-Identifier: Apache-2.0
// node-infra-runtime owns only the system-container deployment boundary.
// Native Connector configuration decoding and switch teardown remain upstream.
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"syscall"
	"time"

	"github.com/kuasar-sandbox/connector/pkg/vswitch"
	"github.com/vishvananda/netlink"
	"github.com/vishvananda/netns"
	"golang.org/x/sys/unix"
)

const (
	recordPath    = "/run/node-infra/recovery/connector.json"
	namespaceRoot = "/run/netns"
	bpfRoot       = "/sys/fs/bpf"
	connector     = "/opt/node-infra/bin/connector-ctl"
)

type namespace struct {
	Name        string `json:"name"`
	Inode       uint64 `json:"inode"`
	Device      uint64 `json:"device,omitempty"`
	External    bool   `json:"external,omitempty"`
	InputInode  uint64 `json:"input_inode,omitempty"`
	InputDevice uint64 `json:"input_device,omitempty"`
}

type transit struct {
	Name            string `json:"name"`
	MAC             string `json:"mac,omitempty"`
	OriginalIfindex int    `json:"original_ifindex,omitempty"`
}

type management struct {
	Namespace    string   `json:"namespace"`
	Device       string   `json:"device"`
	SwitchDevice string   `json:"switch_device,omitempty"`
	Loopback     bool     `json:"loopback"`
	Addresses    []string `json:"addresses,omitempty"`
}

// Record is also the read-only describe output. It is written before namespace
// creation or transit handoff, and survives destruction of the container layer.
type record struct {
	Schema         int          `json:"schema"`
	Generation     string       `json:"generation,omitempty"`
	BootID         string       `json:"boot_id,omitempty"`
	Retained       bool         `json:"retained,omitempty"`
	ConsumerUnits  []string     `json:"consumer_units,omitempty"`
	State          string       `json:"state"`
	SwitchName     string       `json:"switch_name"`
	SwitchNetNS    string       `json:"switch_netns"`
	PortNetNS      string       `json:"port_netns"`
	Namespaces     []namespace  `json:"namespaces"`
	Transit        transit      `json:"transit"`
	Management     []management `json:"management"`
	MainNetNSInode uint64       `json:"main_netns_inode"`
	ConfigPaths    []string     `json:"config_paths"`
	ConfigSHA256   string       `json:"config_sha256"`
}

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "node-infra-runtime:", err)
		os.Exit(1)
	}
}

func run(args []string) error {
	if len(args) == 0 {
		return errors.New("usage: node-infra-runtime describe|prepare|ready|cleanup [--config PATH[:PATH...]] [--force]")
	}
	command := args[0]
	flags := flag.NewFlagSet(command, flag.ContinueOnError)
	config := flags.String("config", "", "explicit native Connector configuration list")
	force := flags.Bool("force", false, "cleanup after the instance and all consumers have ended")
	timeout := flags.Duration("timeout", 120*time.Second, "readiness timeout")
	retained := flags.Bool("retain-handles", false, "retain prepared handles for outer recovery")
	internal := flags.Bool("internal", false, "normal in-container lifecycle cleanup")
	primary := flags.Uint64("primary-netns-inode", 0, "expected host preparation namespace")
	if err := flags.Parse(args[1:]); err != nil {
		return err
	}
	if flags.NArg() != 0 {
		return fmt.Errorf("unexpected arguments: %v", flags.Args())
	}
	if *primary != 0 {
		current, err := inode("/proc/self/ns/net")
		if err != nil || current != *primary {
			return errors.New("helper joined another primary namespace")
		}
	}
	if *force && command != "cleanup" {
		return errors.New("--force is valid only for cleanup")
	}
	if command == "drain" {
		return drain(*timeout)
	}
	if command == "health" {
		return health()
	}
	if command == "budget" {
		return budget()
	}
	if command == "describe" || command == "preflight" {
		r, err := describe(*config)
		if err != nil {
			return err
		}
		if command == "preflight" {
			if err := preflight(r); err != nil {
				return err
			}
			r.MainNetNSInode, err = inode("/proc/self/ns/net")
			if err != nil {
				return err
			}
		}
		return json.NewEncoder(os.Stdout).Encode(r)
	}
	if command == "check" {
		return checkConfiguration(*config)
	}
	if command != "prepare" && command != "ready" && command != "cleanup" {
		return fmt.Errorf("unknown command %q", command)
	}
	if os.Geteuid() != 0 {
		return errors.New("runtime preparation and cleanup require root")
	}
	if err := os.MkdirAll(filepath.Dir(recordPath), 0700); err != nil {
		return err
	}
	lock, err := os.OpenFile(filepath.Join(filepath.Dir(recordPath), ".lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return err
	}
	defer lock.Close()
	if err := unix.Flock(int(lock.Fd()), unix.LOCK_EX); err != nil {
		return err
	}
	defer unix.Flock(int(lock.Fd()), unix.LOCK_UN)
	switch command {
	case "prepare":
		return prepare(*config, *retained)
	case "ready":
		return ready(*timeout)
	case "cleanup":
		return cleanup(*force, *internal)
	}
	return nil
}

func configPaths(explicit string) ([]string, error) {
	if explicit != "" {
		paths := strings.Split(explicit, ":")
		for _, path := range paths {
			if path == "" {
				return nil, errors.New("empty configuration path")
			}
		}
		return paths, nil
	}
	fragments, err := filepath.Glob("/etc/connector-vswitch/conf.d/*.yaml")
	if err != nil {
		return nil, err
	}
	sort.Strings(fragments)
	return append([]string{"/etc/connector-vswitch/vswitch.yaml"}, fragments...), nil
}

func safeName(name string) error {
	if name == "" || name == "." || name == ".." || strings.ContainsAny(name, "/\x00\r\n\t ") {
		return fmt.Errorf("resource name %q is not a single path component", name)
	}
	return nil
}

func describe(explicit string) (*record, error) {
	paths, err := configPaths(explicit)
	if err != nil {
		return nil, err
	}
	// Use the selected Connector revision's parser, including its defaulting,
	// JSON/YAML compatibility and ordered overlay semantics.
	cfg, err := vswitch.LoadConfigFile(strings.Join(paths, ":"))
	if err != nil {
		return nil, err
	}
	if err := cfg.ValidateReserved(); err != nil {
		return nil, err
	}
	if err := safeName(cfg.Name); err != nil {
		return nil, err
	}
	r := &record{Schema: 2, State: "described", SwitchName: cfg.Name, SwitchNetNS: cfg.SwitchNetNS, PortNetNS: cfg.PortNetNS, Transit: transit{Name: cfg.TransitDev}, ConfigPaths: paths}
	names := map[string]bool{}
	for _, name := range []string{cfg.SwitchNetNS, cfg.PortNetNS} {
		if name != "" {
			names[name] = true
		}
	}
	for i, plane := range cfg.MgmtExtracts {
		if plane.NetNS != "" {
			names[plane.NetNS] = true
		}
		if err := safeName(plane.Dev); err != nil {
			return nil, err
		}
		loopback := false
		addresses := map[string]bool{}
		for _, svc := range cfg.MgmtServices {
			for _, route := range plane.ServiceRoutes {
				if svc.TargetIP.IsLoopback() && route.Contains(svc.VIP) {
					loopback = true
					addresses[svc.VIP.String()+"/32"] = true
				}
			}
		}
		m := management{Namespace: plane.NetNS, Device: plane.Dev, SwitchDevice: cfg.MgmtDeviceName(i), Loopback: loopback}
		for address := range addresses {
			m.Addresses = append(m.Addresses, address)
		}
		sort.Strings(m.Addresses)
		r.Management = append(r.Management, m)
	}
	for name := range names {
		if err := safeName(name); err != nil {
			return nil, err
		}
		r.Namespaces = append(r.Namespaces, namespace{Name: name})
	}
	sort.Slice(r.Namespaces, func(i, j int) bool { return r.Namespaces[i].Name < r.Namespaces[j].Name })
	if cfg.TransitDev != "" {
		if err := safeName(cfg.TransitDev); err != nil {
			return nil, err
		}
	}
	hash := sha256.New()
	for _, path := range paths {
		data, err := os.ReadFile(path)
		if err != nil {
			return nil, err
		}
		hash.Write([]byte(path))
		hash.Write([]byte{0})
		hash.Write(data)
		hash.Write([]byte{0})
	}
	r.ConfigSHA256 = hex.EncodeToString(hash.Sum(nil))
	return r, nil
}

func readRecord() (*record, error) {
	data, err := os.ReadFile(recordPath)
	if err != nil {
		return nil, err
	}
	var r record
	if err := json.Unmarshal(data, &r); err != nil {
		return nil, err
	}
	if r.Schema != 1 && r.Schema != 2 {
		return nil, fmt.Errorf("unsupported recovery record schema %d", r.Schema)
	}
	if r.Schema == 2 {
		generation, err := hex.DecodeString(r.Generation)
		if err != nil || len(generation) != 16 || len(r.Generation) != 32 {
			return nil, errors.New("invalid recovery generation")
		}
		if err := safeName(r.BootID); err != nil {
			return nil, err
		}
		switch r.State {
		case "preparing", "prepared", "running", "stopped", "releasing", "cleaned":
		default:
			return nil, errors.New("invalid recovery state")
		}
	}
	if err := safeName(r.SwitchName); err != nil {
		return nil, err
	}
	if r.SwitchNetNS == "" {
		return nil, errors.New("recovery record has no switch namespace")
	}
	for _, ns := range r.Namespaces {
		if err := safeName(ns.Name); err != nil {
			return nil, err
		}
	}
	if err := safeName(r.SwitchNetNS); err != nil {
		return nil, err
	}
	if r.Transit.Name != "" {
		if err := safeName(r.Transit.Name); err != nil {
			return nil, err
		}
	}
	return &r, nil
}

func checkConfiguration(explicit string) error {
	r, err := readRecord()
	if err != nil {
		return err
	}
	if err := validateOwnership(r, false); err != nil {
		return err
	}
	if r.State != "prepared" && r.State != "running" {
		return fmt.Errorf("instance resource state is %s", r.State)
	}
	wanted, err := describe(explicit)
	if err != nil {
		return err
	}
	// Ordinary component restart keeps its switch. Changing the resource
	// identities requires an instance replacement so old resources are not
	// abandoned under a different switch name or namespace.
	if r.SwitchName != wanted.SwitchName || r.SwitchNetNS != wanted.SwitchNetNS || r.PortNetNS != wanted.PortNetNS || r.Transit.Name != wanted.Transit.Name || len(r.Namespaces) != len(wanted.Namespaces) || len(r.Management) != len(wanted.Management) {
		return errors.New("Connector resource identities changed; replace the instance after cleanup")
	}
	for i := range r.Namespaces {
		if r.Namespaces[i].Name != wanted.Namespaces[i].Name {
			return errors.New("Connector namespace identities changed; replace the instance after cleanup")
		}
		value, err := inode(filepath.Join(namespaceRoot, r.Namespaces[i].Name))
		if err != nil {
			return err
		}
		if r.Namespaces[i].Inode == 0 || value != r.Namespaces[i].Inode {
			return errors.New("Connector namespace pin changed")
		}
	}
	for i := range r.Management {
		if r.Management[i].Namespace != wanted.Management[i].Namespace || r.Management[i].Device != wanted.Management[i].Device || strings.Join(r.Management[i].Addresses, ",") != strings.Join(wanted.Management[i].Addresses, ",") {
			return errors.New("Connector management link identities changed; replace the instance after cleanup")
		}
	}
	current, err := inode("/proc/self/ns/net")
	if err != nil {
		return err
	}
	if current != r.MainNetNSInode {
		return errors.New("instance main network namespace changed")
	}
	return verifyTransit(r, false)
}

func writeRecord(r *record) error {
	data, err := json.MarshalIndent(r, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	f, err := os.CreateTemp(filepath.Dir(recordPath), ".connector-")
	if err != nil {
		return err
	}
	path := f.Name()
	defer os.Remove(path)
	if err := f.Chmod(0600); err != nil {
		f.Close()
		return err
	}
	if _, err := f.Write(data); err != nil {
		f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	if err := os.Rename(path, recordPath); err != nil {
		return err
	}
	dir, err := os.Open(filepath.Dir(recordPath))
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}

func inode(path string) (uint64, error) {
	info, err := os.Stat(path)
	if err != nil {
		return 0, err
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return 0, fmt.Errorf("cannot inspect inode of %s", path)
	}
	return stat.Ino, nil
}

func execute(program string, args ...string) error {
	cmd := exec.Command(program, args...)
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	return cmd.Run()
}

func prepare(explicit string, retained bool) error {
	old, err := readRecord()
	if err == nil && old.State != "cleaned" {
		// Host preparation pins namespaces before PID 1 starts. The system
		// container only verifies that exact generation and configuration.
		if old.State == "prepared" {
			wanted, err := describe(explicit)
			if err != nil {
				return err
			}
			if old.ConfigSHA256 != wanted.ConfigSHA256 {
				return errors.New("configuration changed after host preparation")
			}
			return checkConfiguration(explicit)
		}
		return errors.New("previous Connector generation needs cleanup before preparation")
	}
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	r, err := describe(explicit)
	if err != nil {
		return err
	}
	if r.SwitchNetNS == "" {
		return errors.New("node-infra requires an internal switch namespace")
	}
	// The host mounts a private bpffs; mounting an unrecorded replacement here
	// would hide pins from the host's recovery container.
	var fs unix.Statfs_t
	if err := unix.Statfs(bpfRoot, &fs); err != nil {
		return err
	}
	if fs.Type != 0xcafe4a11 {
		return errors.New("host-provided /sys/fs/bpf is not bpffs")
	}
	if _, err := os.Stat(filepath.Join(bpfRoot, r.SwitchName)); err == nil {
		return errors.New("switch pins already exist; recover the prior generation")
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if err := os.MkdirAll(namespaceRoot, 0755); err != nil {
		return err
	}
	if err := externalInputs(r); err != nil {
		return err
	}
	for _, ns := range r.Namespaces {
		if _, err := os.Lstat(filepath.Join(namespaceRoot, ns.Name)); err == nil {
			return fmt.Errorf("namespace pin %s already exists; recover it before preparation", ns.Name)
		} else if !errors.Is(err, os.ErrNotExist) {
			return err
		}
	}
	r.MainNetNSInode, err = inode("/proc/self/ns/net")
	if err != nil {
		return err
	}
	if err := preflight(r); err != nil {
		return err
	}
	if r.Transit.Name != "" {
		link, err := netlink.LinkByName(r.Transit.Name)
		if err != nil {
			return fmt.Errorf("transit input: %w", err)
		}
		if link.Attrs().Flags&net.FlagUp != 0 {
			return fmt.Errorf("transit %s must be DOWN before Connector handoff", r.Transit.Name)
		}
		r.Transit.MAC, r.Transit.OriginalIfindex = link.Attrs().HardwareAddr.String(), link.Attrs().Index
		if r.Transit.MAC == "" {
			return errors.New("transit has no stable hardware address for recovery")
		}
	}
	r.Retained = retained
	if err := initializeOwnership(r); err != nil {
		return err
	}
	r.State = "preparing"
	if err := writeRecord(r); err != nil {
		return err
	}
	if err := publishOwnership(r); err != nil {
		return err
	}
	if err := ensureNamespaceMount(); err != nil {
		return err
	}
	for i := range r.Namespaces {
		if err := publishNamespace(r.Namespaces[i]); err != nil {
			return err
		}
		r.Namespaces[i].Device, r.Namespaces[i].Inode, err = namespaceIdentity(filepath.Join(namespaceRoot, r.Namespaces[i].Name))
		if err != nil {
			return err
		}
		if err := writeRecord(r); err != nil {
			return err
		}
		if r.Namespaces[i].External {
			continue
		}
		if err := execute("ip", "-n", r.Namespaces[i].Name, "link", "set", "lo", "up"); err != nil {
			return err
		}
	}
	r.State = "prepared"
	return writeRecord(r)
}

func ready(timeout time.Duration) error {
	if timeout <= 0 {
		return errors.New("readiness timeout must be positive")
	}
	r, err := readRecord()
	if err != nil {
		return err
	}
	if err := validateOwnership(r, false); err != nil {
		return err
	}
	if r.State != "prepared" && r.State != "running" {
		return fmt.Errorf("unexpected state %s", r.State)
	}
	deadline := time.Now().Add(timeout)
	var last error
	for time.Now().Before(deadline) {
		out, err := exec.Command(connector, "vswitch", "status", "--", r.SwitchName).Output()
		if err == nil {
			var status struct {
				PortsAvailable uint32 `json:"ports_available"`
				PortsUsed      uint32 `json:"ports_used"`
				Conditions     []struct {
					Type   string `json:"type"`
					Status string `json:"status"`
				} `json:"conditions"`
			}
			if err = json.Unmarshal(out, &status); err == nil {
				healthy := false
				for _, c := range status.Conditions {
					if c.Type == "Ready" && c.Status == "True" {
						healthy = true
					}
				}
				if healthy && status.PortsAvailable+status.PortsUsed > 0 {
					if err := validateNamespaces(r); err != nil {
						return err
					}
					for _, m := range r.Management {
						if !m.Loopback {
							continue
						}
						if err := verifyManagementPeer(r, m); err != nil {
							return err
						}
						// Connector installs the floating return route but does not
						// assign local service addresses. Without a local VIP here,
						// Proxy uses the ingress source and guest replies take transit.
						for _, address := range m.Addresses {
							args := []string{"address", "replace", address, "dev", m.Device}
							if m.Namespace != "" {
								args = append([]string{"-n", m.Namespace}, args...)
							}
							if err := execute("ip", args...); err != nil {
								return err
							}
						}
						args := []string{"-w", "net/ipv4/conf/" + m.Device + "/route_localnet=1"}
						if m.Namespace == "" {
							err = planeSysctl("", args)
						} else {
							err = planeSysctl(m.Namespace, args)
						}
						if err != nil {
							return err
						}
					}
					r.State = "running"
					return writeRecord(r)
				}
				err = errors.New("switch is not healthy or has no provisioned capacity")
			}
		}
		last = err
		time.Sleep(200 * time.Millisecond)
	}
	return fmt.Errorf("Connector readiness timeout: %w", last)
}

func legacyConsumersGone() error {
	for _, name := range []string{"sandbox-runner.slice", "sandbox-builder.slice"} {
		path := filepath.Join("/sys/fs/cgroup/sandbox.slice", name, "cgroup.events")
		data, err := os.ReadFile(path)
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			return err
		}
		if strings.Contains(string(data), "populated 1") {
			return fmt.Errorf("consumer slice %s is still populated", name)
		}
	}
	return nil
}

func validateNamespaces(r *record) error {
	for _, ns := range r.Namespaces {
		value, err := inode(filepath.Join(namespaceRoot, ns.Name))
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			return err
		}
		if r.Schema >= 2 && (ns.Inode != 0 || ns.External) {
			var fs unix.Statfs_t
			if err := unix.Statfs(filepath.Join(namespaceRoot, ns.Name), &fs); err != nil {
				return err
			}
			if ns.Inode == 0 && fs.Type != unix.NSFS_MAGIC {
				continue
			} // reserved mount target before bind
			dev, ino, err := namespaceIdentity(filepath.Join(namespaceRoot, ns.Name))
			if err != nil {
				return err
			}
			if ns.External && (dev != ns.InputDevice || ino != ns.InputInode) {
				return fmt.Errorf("external namespace %s changed", ns.Name)
			}
			if ns.Inode != 0 && (dev != ns.Device || ino != ns.Inode) {
				return fmt.Errorf("namespace %s changed", ns.Name)
			}
		}
		// Inode zero records a crash between creating the run-owned namespace
		// and persisting its inode. Its name was reserved in the private pin
		// directory before creation; no other instance shares that directory.
		if ns.Inode != 0 && value != ns.Inode {
			return fmt.Errorf("namespace %s was replaced; retain recovery handles", ns.Name)
		}
	}
	return nil
}

func lookupTransit(r *record) (netlink.Link, netlink.Link, *netlink.Handle, error) {
	main, err := netlink.LinkByName(r.Transit.Name)
	if err != nil {
		var missing netlink.LinkNotFoundError
		if !errors.As(err, &missing) {
			return nil, nil, nil, err
		}
		main = nil
	}
	ns, err := netns.GetFromName(r.SwitchNetNS)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return main, nil, nil, nil
		}
		return nil, nil, nil, err
	}
	defer ns.Close()
	h, err := netlink.NewHandleAt(ns)
	if err != nil {
		return nil, nil, nil, err
	}
	old, err := h.LinkByName(r.Transit.Name)
	if err != nil {
		var missing netlink.LinkNotFoundError
		if !errors.As(err, &missing) {
			h.Close()
			return nil, nil, nil, err
		}
		old = nil
	}
	return main, old, h, nil
}

func verifyTransit(r *record, restore bool) error {
	if r.Transit.Name == "" {
		return nil
	}
	main, old, handle, err := lookupTransit(r)
	if err != nil {
		return err
	}
	if handle != nil {
		defer handle.Close()
	}
	if old != nil {
		if old.Attrs().HardwareAddr.String() != r.Transit.MAC {
			return errors.New("switch transit identity differs from the applied record")
		}
		if main != nil {
			return errors.New("main namespace has a conflicting transit name; retain the original switch and pins")
		}
		if !restore {
			return nil
		}
		caller, err := netns.Get()
		if err != nil {
			return err
		}
		defer caller.Close()
		if err := handle.LinkSetDown(old); err != nil {
			return err
		}
		if err := handle.LinkSetNsFd(old, int(caller)); err != nil {
			return err
		}
		main, err = netlink.LinkByName(r.Transit.Name)
		if err != nil {
			return err
		}
	}
	if main == nil {
		return errors.New("original transit is absent from both retained namespaces")
	}
	if main.Attrs().HardwareAddr.String() != r.Transit.MAC {
		return errors.New("returned transit identity differs from the applied record")
	}
	if restore {
		return netlink.LinkSetDown(main)
	}
	return nil
}

func cleanup(force, internal bool) error {
	r, err := readRecord()
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	if r.State == "cleaned" {
		return nil
	}
	current, err := inode("/proc/self/ns/net")
	if err != nil {
		return err
	}
	if r.MainNetNSInode != current {
		return errors.New("cleanup must run in the original instance main network namespace")
	}
	if err := validateOwnership(r, true); err != nil {
		return err
	}
	if err := consumersGone(r); err != nil {
		return err
	}
	if err := validateNamespaces(r); err != nil {
		return err
	}
	// A killed namespace publisher may leave its reserved, empty mount target.
	// Remove it before native Stop or transit/peer lookup tries to enter it.
	// validateNamespaces and the boot/generation owner guard run first.
	for _, ns := range r.Namespaces {
		if ns.Inode != 0 {
			continue
		}
		path := filepath.Join(namespaceRoot, ns.Name)
		var fs unix.Statfs_t
		if err := unix.Statfs(path, &fs); errors.Is(err, os.ErrNotExist) {
			continue
		} else if err != nil {
			return err
		}
		if fs.Type != unix.NSFS_MAGIC {
			info, err := os.Lstat(path)
			if err != nil || !info.Mode().IsRegular() || info.Size() != 0 {
				return fmt.Errorf("invalid reserved namespace target %s", path)
			}
			if err := os.Remove(path); err != nil {
				return err
			}
		}
	}
	// Check conflicts before native Stop: native stop can ignore EEXIST and
	// unpin metadata even when its transit device did not return.
	if err := verifyTransit(r, false); err != nil {
		return err
	}
	args := []string{"vswitch", "stop"}
	if force {
		args = append(args, "--force")
	}
	args = append(args, "--", r.SwitchName)
	stopErr := execute(connector, args...)
	var exit *exec.ExitError
	absent := errors.As(stopErr, &exit) && exit.ExitCode() == 3
	if stopErr != nil && !absent {
		return fmt.Errorf("native stop did not complete: %w", stopErr)
	}
	// The saved names and identities also cover startup rollback that already
	// removed BPF metadata. The host authorizes --force only after old consumers
	// have ended; normal shutdown uses the safe native stop path above.
	if err := verifyTransit(r, true); err != nil {
		return err
	}
	if err := removeManagementLinks(r); err != nil {
		return err
	}
	if err := os.RemoveAll(filepath.Join(bpfRoot, r.SwitchName)); err != nil {
		return err
	}
	if internal && r.Retained {
		r.State = "stopped"
		if err := writeRecord(r); err != nil {
			return err
		}
		fmt.Println("node-infra: network cleanup complete (outer handles retained)")
		return nil
	}
	for i := len(r.Namespaces) - 1; i >= 0; i-- {
		name := r.Namespaces[i].Name
		if _, err := os.Stat(filepath.Join(namespaceRoot, name)); errors.Is(err, os.ErrNotExist) {
			continue
		} else if err != nil {
			return err
		}
		var fs unix.Statfs_t
		if r.Namespaces[i].Inode == 0 && unix.Statfs(filepath.Join(namespaceRoot, name), &fs) == nil && fs.Type != unix.NSFS_MAGIC {
			if err := os.Remove(filepath.Join(namespaceRoot, name)); err != nil {
				return err
			}
			continue
		}
		if err := execute("ip", "netns", "delete", name); err != nil {
			return err
		}
		if _, err := os.Lstat(filepath.Join(namespaceRoot, name)); !errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("namespace pin %s remains after deletion", name)
		}
	}
	if err := verifyTransit(r, true); err != nil {
		return err
	}
	if r.Schema >= 2 {
		r.State = "releasing"
		if err := writeRecord(r); err != nil {
			return err
		}
		if err := os.Remove(filepath.Join(namespaceRoot, ".node-infra-owner-"+r.Generation)); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
		if err := os.Remove(filepath.Join(namespaceRoot, ".node-infra-owner")); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
	}
	r.State = "cleaned"
	if err := writeRecord(r); err != nil {
		return err
	}
	fmt.Println("node-infra: network cleanup complete")
	return nil
}
