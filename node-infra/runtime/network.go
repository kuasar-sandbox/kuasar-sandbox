// SPDX-License-Identifier: Apache-2.0
package main

import (
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"os/exec"
	"strings"

	"github.com/kuasar-sandbox/connector/pkg/vswitch"
	"github.com/vishvananda/netlink"
	"github.com/vishvananda/netns"
	"gopkg.in/yaml.v3"
)

// Match Connector's return-route span, including unaligned bases. Decode the
// selected native configuration instead of interpreting YAML independently.
func returnNetworks(cfg *vswitch.Config) []*net.IPNet {
	base := binary.BigEndian.Uint32(cfg.FloatingIPBase.To4())
	span := uint64(1) << (12 + cfg.GenerationBits)
	mask := net.CIDRMask(20-int(cfg.GenerationBits), 32)
	end := make(net.IP, 4)
	binary.BigEndian.PutUint32(end, uint32(uint64(base)+span-1))
	first := &net.IPNet{IP: cfg.FloatingIPBase.Mask(mask), Mask: mask}
	result := []*net.IPNet{first}
	if !first.Contains(end) {
		result = append(result, &net.IPNet{IP: end.Mask(mask), Mask: mask})
	}
	return result
}

func overlaps(a, b *net.IPNet) bool { return a.Contains(b.IP) || b.Contains(a.IP) }

func preflight(r *record) error {
	cfg, err := vswitch.LoadConfigFile(strings.Join(r.ConfigPaths, ":"))
	if err != nil {
		return err
	}
	links, err := netlink.LinkList()
	if err != nil {
		return err
	}
	var wanted []*net.IPNet
	for _, m := range r.Management {
		// Named management namespaces are created in the instance-private pin
		// directory. Only the caller namespace can contain pre-existing peers.
		if m.Namespace != "" {
			continue
		}
		for _, link := range links {
			if link.Attrs().Name == m.Device {
				return fmt.Errorf("management interface %s already exists in primary namespace", m.Device)
			}
		}
		wanted = append(wanted, returnNetworks(cfg)...)
		for _, address := range m.Addresses {
			_, subnet, err := net.ParseCIDR(address)
			if err != nil {
				return err
			}
			wanted = append(wanted, subnet)
		}
	}
	if len(wanted) != 0 {
		routes, err := netlink.RouteListFiltered(netlink.FAMILY_V4, &netlink.Route{}, netlink.RT_FILTER_TABLE)
		if err != nil {
			return err
		}
		for _, route := range routes {
			if route.Dst == nil {
				continue // existing default egress is not an owned management route
			}
			if prefix, _ := route.Dst.Mask.Size(); prefix == 0 {
				continue
			}
			for _, subnet := range wanted {
				if overlaps(route.Dst, subnet) {
					return fmt.Errorf("management network %s conflicts with existing route %s (table %d)", subnet, route.Dst, route.Table)
				}
			}
		}
		addresses, err := netlink.AddrList(nil, netlink.FAMILY_V4)
		if err != nil {
			return err
		}
		for _, address := range addresses {
			for _, subnet := range wanted {
				if overlaps(address.IPNet, subnet) {
					return fmt.Errorf("management network %s conflicts with existing address %s", subnet, address.IPNet)
				}
			}
		}
	}
	return checkListeners()
}

// Probe ordinary native TCP listeners, using the native CLI's normalized output.
// SDK Configure hooks and inherited fd expressions remain native startup checks.
func checkListeners() error {
	var conductor struct {
		API struct {
			Listen string `yaml:"listen"`
		} `yaml:"api"`
		MMDS struct {
			Enabled bool   `yaml:"enabled"`
			Listen  string `yaml:"listen"`
		} `yaml:"mmds"`
		Proxy struct {
			MetricsListen string `yaml:"metrics_listen"`
		} `yaml:"proxy"`
	}
	var proxy struct {
		DataListen    string `yaml:"data_listen"`
		MetricsListen string `yaml:"metrics_listen"`
		ProxyNetNS    string `yaml:"proxy_netns"`
	}
	for role, target := range map[string]any{"conductor": &conductor, "proxy": &proxy} {
		output, err := exec.Command("/usr/libexec/node-infra/service", "config", role).Output()
		if err != nil {
			return fmt.Errorf("native %s configuration: %w", role, err)
		}
		if err := yaml.Unmarshal(output, target); err != nil {
			return err
		}
	}
	addresses := []string{conductor.API.Listen, conductor.Proxy.MetricsListen, proxy.DataListen, proxy.MetricsListen}
	if conductor.MMDS.Enabled && proxy.ProxyNetNS == "" {
		addresses = append(addresses, conductor.MMDS.Listen)
	}
	return probeListeners(addresses)
}

func probeListeners(addresses []string) error {
	var listeners []net.Listener
	defer func() {
		for _, listener := range listeners {
			listener.Close()
		}
	}()
	for _, address := range addresses {
		if address == "" || strings.HasPrefix(address, "fd=") {
			continue
		}
		listener, err := net.Listen("tcp", address)
		if err != nil {
			return fmt.Errorf("native listener %s unavailable in primary namespace: %w", address, err)
		}
		listeners = append(listeners, listener)
	}
	return nil
}

// Before assigning VIPs or route_localnet, prove the named device is the peer
// of this generation's switch veth. Cleanup removes that private switch end;
// it never deletes a primary interface by a configured name.
func verifyManagementPeer(r *record, m management) error {
	if m.SwitchDevice == "" {
		return nil
	} // schema-1 records from older images
	switchNS, err := netns.GetFromName(r.SwitchNetNS)
	if err != nil {
		return err
	}
	defer switchNS.Close()
	managementNS, err := netns.Get()
	if m.Namespace != "" {
		if err == nil {
			managementNS.Close()
		}
		managementNS, err = netns.GetFromName(m.Namespace)
	}
	if err != nil {
		return err
	}
	defer managementNS.Close()
	sw, err := netlink.NewHandleAt(switchNS)
	if err != nil {
		return err
	}
	defer sw.Close()
	mgmt, err := netlink.NewHandleAt(managementNS)
	if err != nil {
		return err
	}
	defer mgmt.Close()
	peer, err := sw.LinkByName(m.SwitchDevice)
	if err != nil {
		return err
	}
	link, err := mgmt.LinkByName(m.Device)
	if err != nil {
		return err
	}
	// Connector also allows both veth ends in the same private namespace.
	// That topology has no cross-namespace ID, but still has reciprocal indexes.
	if !switchNS.Equal(managementNS) {
		nsid, err := mgmt.GetNetNsIdByFd(int(switchNS))
		if err != nil {
			return err
		}
		if nsid < 0 || link.Attrs().NetNsID != nsid {
			return errors.New("management peer belongs to another network namespace")
		}
	}
	if peer.Type() != "veth" || link.Type() != "veth" ||
		peer.Attrs().ParentIndex != link.Attrs().Index || link.Attrs().ParentIndex != peer.Attrs().Index {
		return errors.New("management interface is not the retained switch's peer; refusing address changes")
	}
	return nil
}
