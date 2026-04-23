#!/usr/bin/env python3
"""Applies or removes config on Cisco NX-OS for RoCEv2 traffic.
Can run directly on the switch, like:
n9k# python3 bootflash:roce_enable.py
Or remotely on a Linux machine, like:
python3 roce_enable.py --switch-file nexus_switches.txt
"""

__author__ = "Paresh Gupta"
__version__ = "0.50"
__updated__ = "17-Apr-2026-1-PM-PDT"

import sys
import argparse
import time
import re
from utils import nxos_utils

SPINE_ASN=65001
LEAF_ASN=64601
leaf_cnt, spine_cnt = 0,0
LEAF_LOOPBACK_PREFIX='10.0.'
SPINE_LOOPBACK_PREFIX='10.1.'

def parse_cmdline_arguments():
    desc_str = (
        "Apply NX-OS Modular Quality of Service (QoS) Command-Line Interface\n"
        "(CLI) (MQC) for handling RoCEv2 traffic. This is a wrapper\n"
        "for applying PFC and ECN config. It covers the most common\n"
        "scenario, but not all cases. For customization, change this\n"
        "script, use the options below, or use NX-OS CLI directly.\n"
        f"V:{__version__} ({__updated__})"
    )

    base_parser = nxos_utils.get_base_parser()
    parser = argparse.ArgumentParser(parents=[base_parser],
                description=desc_str,
                formatter_class=argparse.RawDescriptionHelpFormatter,
                )

    return parser.parse_args()

def assign_bgp_asn(args, fabric_topology):
    switchname_dict = {}
    switchname_dict['spine'] = {}
    switchname_dict['leaf'] = {}
    spine_dict = {}
    leaf_dict = {}
    for switch_ip, switch_attr in fabric_topology.items():
        if re.search('leaf', switch_attr["hostname"], re.IGNORECASE):
            leaf_dict[switch_attr["hostname"]] = {}
            leaf_dict[switch_attr["hostname"]]["switch_ip"] = switch_ip
        if re.search('spine', switch_attr["hostname"], re.IGNORECASE):
            spine_dict[switch_attr["hostname"]] = {}
            spine_dict[switch_attr["hostname"]]["switch_ip"] = switch_ip

    switchname_dict['leaf'] = dict(sorted(leaf_dict.items()))
    leaf_cnt = 0
    for switch_name, switch_attr in switchname_dict['leaf'].items():
        switch_attr["asn"] = LEAF_ASN + leaf_cnt
        leaf_cnt = leaf_cnt + 1
    spine_cnt = 0
    switchname_dict['spine'] = dict(sorted(spine_dict.items()))
    for switch_name, switch_attr in switchname_dict['spine'].items():
        switch_attr["asn"] = SPINE_ASN + spine_cnt
        spine_cnt = spine_cnt + 1

    switchname_dict = switchname_dict['leaf'] | switchname_dict['spine']
    return switchname_dict

def apply_config(args, host_os, switch_ip, switchuser, fabric_topology, switch_asn_dict):
    asn = 0
    loopback_ip = ''
    global_commands = f"""
conf
feature bgp
route-map redis-map permit 10
  match tag 12345
"""

    if re.search('leaf', fabric_topology[switch_ip]["hostname"], re.IGNORECASE):
        loopback_ip = LEAF_LOOPBACK_PREFIX + \
                        switch_ip.split('.')[-2] + '.' + switch_ip.split('.')[-1]
        asn = switch_asn_dict[fabric_topology[switch_ip]["hostname"]]['asn']
    elif re.search('spine', fabric_topology[switch_ip]["hostname"], re.IGNORECASE):
        loopback_ip = SPINE_LOOPBACK_PREFIX + \
                        switch_ip.split('.')[-2] + '.' + switch_ip.split('.')[-1]
        asn = switch_asn_dict[fabric_topology[switch_ip]["hostname"]]['asn']
    else:
        print(f'Error: Switch: {switch_ip} must have leaf or spine in its hostname ')
        return

    loopback_commands = f"""
interface loopback0
 ip address {loopback_ip}/32 tag 12345
"""
    router_commands = f"""
router bgp {asn}
 router-id {loopback_ip}
 address-family ipv4 unicast
  redistribute direct route-map redis-map
  maximum-paths 128
 address-family ipv6 unicast
  redistribute direct route-map redis-map
  maximum-paths 128
"""

    intf_dict = fabric_topology[switch_ip]["intf"]
    neighbor_command = ''
    intf_command = ''
    for intf, intf_attr in intf_dict.items():
        if 'mgmt' in intf:
            continue
        if 'switch' not in intf_attr["meta"]["neighbor_type"]:
            continue
        neighbor_asn = switch_asn_dict[intf_attr["meta"]["neighbor_name"]]['asn']
        neighbor_command = neighbor_command + \
                            f"""
 neighbor {intf}
  remote-as {neighbor_asn}
  address-family ipv4 unicast
  address-family ipv6 unicast
"""

        intf_command = intf_command + \
                        f"""
interface {intf}
 ipv6 address use-link-local-only
 ip forward
"""

    commands = global_commands + intf_command + loopback_commands + router_commands + neighbor_command + '\nend\n'
    if args.print_only:
        print('----------------------------------------')
        print(f"INFO: Switch: {switch_ip}: Following is the config in pretty format:\n")
        print("+++")
        print(commands.strip())
        print("+++")
        print("\nINFO: Following is the config format to be sent to the switch:")
        print("+++")
        print(nxos_utils.normalize_cli_blob(commands))
        print("+++")
        print("\n")
        return

    try:
        print(f"INFO: Switch: {switch_ip}: Trying to apply config by {switchuser}...")
        nxos_utils.run_cmd(args, commands, host_os, switch_ip, switchuser)
    except Exception as exc:
        print(f"Failed to apply configuration: {exc}")
        return

def remove_config(args, host_os, switch_ip, switchuser, switch_topology):
    if args.intf == "":
        intf_range = nxos_utils.build_interface_range(args, host_os, \
                                                      switch_ip, switchuser)
    else:
        intf_range = args.intf
    if args.print_intf:
        print("INFO: Printing only interface range:")
        print(intf_range)
        return

    intf_commands = f"""
conf
interface {intf_range}
no priority-flow-control mode on
no priority-flow-control watch-dog-interval on
no service-policy type qos input QOS_CLASSIFICATION
exit
"""

    qos_commands = """
no priority-flow-control watch-dog-interval on
system qos
"""

    sys_qos_queuing = "service-policy type queuing output QOS_EGRESS_PORT"
    sys_qos_network = "service-policy type network-qos qos_network"
    c_sys_qos = 'sh run | section "system qos"'
    try:
        sys_qos_applied = nxos_utils.run_cmd(args, c_sys_qos, host_os, \
                                             switch_ip, switchuser).strip()
        if sys_qos_queuing in sys_qos_applied:
            qos_commands += f"no {sys_qos_queuing}\n"
        if sys_qos_network in sys_qos_applied:
            qos_commands += f"no {sys_qos_network}\n"
    except Exception:
        pass

    qos_commands += """
exit
no policy-map type queuing QOS_EGRESS_PORT
no policy-map type network-qos qos_network
no policy-map type qos QOS_CLASSIFICATION
no class-map type qos match-any CNP
no class-map type qos match-any ROCEv2
end
"""

    commands = intf_commands + qos_commands

    if args.print_only:
        print('----------------------------------------')
        print(f"INFO: Switch: {switch_ip}: Following is the config in pretty format:\n")
        print("+++")
        print(commands.strip())
        print("+++")
        print("\nINFO: Following is the config format to be sent to the switch:")
        print("+++")
        print(nxos_utils.normalize_cli_blob(commands))
        print("+++")
        print("\n")
        return

    try:
        print(f"INFO: Switch: {switch_ip}: Trying to remove config...")
        nxos_utils.run_cmd(args, commands, host_os, switch_ip, switchuser)
    except Exception as exc:
        print(f"Failed to remove configuration: {exc}")
        return

def change_config(args, host_os, switch_ip, switchuser, fabric_topology, switch_asn_dict):
    start_t = time.time()
    if args.disable:
        remove_config(args, host_os, switch_ip, switchuser, fabric_topology, switch_asn_dict)
    else:
        apply_config(args, host_os, switch_ip, switchuser, fabric_topology, switch_asn_dict)
    print(f"INFO: Switch: {switch_ip} took {round((time.time() - start_t), 2)}s")


def main():
    args = parse_cmdline_arguments()
    host_os = nxos_utils.detect_host_os(args)
    if host_os == 'linux':
        switch_dict = {}
        nxos_utils.get_switches(args, switch_dict)

        # Discover the fabric using seed switch from the provided switch-file
        if args.fabric:
            fabric_topology = nxos_utils.get_fabric_topology(args, host_os, switch_dict)
            switch_asn_dict = assign_bgp_asn(args, fabric_topology)
    nxos_utils.common_worker(args, change_config, switch_asn_dict)

if __name__ == "__main__":
    main()
