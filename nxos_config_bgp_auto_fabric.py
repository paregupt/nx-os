#!/usr/bin/env python3
"""Applies or removes BGP config on Cisco NX-OS leaf-spine fabrics for RoCEv2 traffic.
"""

__author__ = "Paresh Gupta"
__version__ = "0.60"
__updated__ = "26-Apr-2026-1-PM-PDT"

import sys
import argparse
import time
import re
import json
import ipaddress
from utils import nxos_utils

SPINE_ASN=65001
LEAF_ASN=64601
LEAF_LOOPBACK_PREFIX='10.0.'
SPINE_LOOPBACK_PREFIX='10.1.'
P2P_IPV4_NET='10.2.0.0/20'
P2P_IPV4_SUBNET=31


def parse_cmdline_arguments():
    desc_str = (
        "Configure BGP fabric on a Cisco NX-OS spine-leaf network\n"
        f"V:{__version__} ({__updated__})"
    )

    base_parser = nxos_utils.get_base_parser()
    parser = argparse.ArgumentParser(parents=[base_parser],
                description=desc_str,
                formatter_class=argparse.RawDescriptionHelpFormatter,
                )

    parser.add_argument(
        "--ebgp_v4p2p",
        default=False,
        action="store_true",
        help=(
            "Configure eBGP with P2P IPv4 addresses in /31 subnets"
        ),
    )
    return parser.parse_args()

def get_p2p_ipv4_address_list():
    return list(ipaddress.ip_network(P2P_IPV4_NET).subnets(new_prefix=P2P_IPV4_SUBNET))

def detect_switch_role(switch_attr):
    if re.search('leaf', switch_attr["hostname"], re.IGNORECASE):
        switch_attr["role"] = "leaf"
    elif re.search('spine', switch_attr["hostname"], re.IGNORECASE):
        switch_attr["role"] = "spine"
    else:
        switch_attr["role"] = 'unknown'

    #TODO: Add logic to detect switch roles based on host+switch vs switch-only neighbors

def assign_bgp_params(args, fabric_topology):
    switch_err = False
    leaf_cnt = 0
    spine_cnt = 0
    isl_cnt = 0
    spine_dict = {} # temp storage of leaf neighbors
    p2p_ipv4_address_list = get_p2p_ipv4_address_list()
    for switch_ip, switch_attr in fabric_topology.items():
        detect_switch_role(switch_attr)
        if 'leaf' in switch_attr["role"]:
            switch_attr["asn"] = LEAF_ASN + leaf_cnt
            switch_attr["ipv4_loopback"] = LEAF_LOOPBACK_PREFIX + switch_ip.split('.')[-2] + '.' + \
                                                                  switch_ip.split('.')[-1]
            leaf_cnt = leaf_cnt + 1
            print(f"Using hostname {switch_attr['hostname']} to set LEAF role with asn:{switch_attr['asn']} "
                  f"for {switch_ip}")

            for intf, intf_attr in switch_attr["intf"].items():
                if 'mgmt' in intf or 'switch' not in intf_attr["meta"]["neighbor_type"]:
                    continue
                intf_attr["meta"]["p2p_ipv4_address"] = str(list(p2p_ipv4_address_list[isl_cnt].hosts())[0])
                intf_attr["meta"]["p2p_neighbor_ipv4_address"] = str(list(p2p_ipv4_address_list[isl_cnt].hosts())[1])
                intf_attr["meta"]["p2p_ipv4_subnet"] = P2P_IPV4_SUBNET
                isl_cnt += 1
                if intf_attr["meta"]["neighbor_address"] not in spine_dict:
                    spine_dict[intf_attr["meta"]["neighbor_address"]] = {}
                spine_dict[intf_attr["meta"]["neighbor_address"]][intf_attr["meta"]["neighbor_intf"]] = {}
                spine_dict[intf_attr["meta"]["neighbor_address"]][intf_attr["meta"]["neighbor_intf"]]["p2p_ipv4_address"] = \
                            intf_attr["meta"]["p2p_neighbor_ipv4_address"]
                spine_dict[intf_attr["meta"]["neighbor_address"]][intf_attr["meta"]["neighbor_intf"]]["p2p_neighbor_ipv4_address"] = \
                            intf_attr["meta"]["p2p_ipv4_address"]
        elif 'spine' in switch_attr["role"]:
            if switch_ip not in spine_dict:
                spine_dict[switch_ip] = {}
            switch_attr["asn"] = SPINE_ASN + spine_cnt
            switch_attr["ipv4_loopback"] = SPINE_LOOPBACK_PREFIX + switch_ip.split('.')[-2] + '.' + \
                                                                   switch_ip.split('.')[-1]
            # keep same asn for all spines
            #spine_cnt = spine_cnt + 1
            print(f"Using hostname {switch_attr['hostname']} to set SPINE role with asn:{switch_attr['asn']} "
                  f"for {switch_ip}")
        else:
            print(f'ERROR: Switch: {switch_ip} unable to detect role')
            switch_err = True
            continue

    if switch_err:
        print('ERROR: Change switch hostnames containing leaf and spines to proceed')
        sys.exit(1)

    if args.more_verbose:
        print(f'isl_cnt: {isl_cnt}/{len(p2p_ipv4_address_list)}, spine_dict:')
        print(json.dumps(spine_dict, indent=2))
    # now fill p2p_ipv4_address on spines using p2p_neighbor_ipv4_address on leaf
    for switch_ip, switch_attr in spine_dict.items():
        for intf, intf_attr in fabric_topology[switch_ip]["intf"].items():
            if 'mgmt' in intf or 'switch' not in intf_attr["meta"]["neighbor_type"]:
                continue
            intf_attr["meta"]["p2p_ipv4_address"] = switch_attr[intf]["p2p_ipv4_address"]
            intf_attr["meta"]["p2p_neighbor_ipv4_address"] = switch_attr[intf]["p2p_neighbor_ipv4_address"]
            intf_attr["meta"]["p2p_ipv4_subnet"] = P2P_IPV4_SUBNET

def apply_config(args, host_os, switch_ip, switchuser, fabric_topology):
    global_commands = """
conf
feature bgp
route-map redis-map permit 10
  match tag 12345
"""

    loopback_commands = f"""
interface loopback0
 ip address {fabric_topology[switch_ip]["ipv4_loopback"]}/32 tag 12345
"""
    router_commands = f"""
router bgp {fabric_topology[switch_ip]["asn"]}
 router-id {fabric_topology[switch_ip]["ipv4_loopback"]}
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
        neighbor_command = neighbor_command + \
                            f"""
 neighbor {intf_attr["meta"]["p2p_neighbor_ipv4_address"]}
  remote-as {fabric_topology[intf_attr["meta"]["neighbor_address"]]['asn']}
  update-source {intf}"""

        if 'leaf' in fabric_topology[switch_ip]["role"]:
            neighbor_command = neighbor_command + """
  address-family ipv4 unicast
    allowas-in 1
  address-family ipv6 unicast
    allowas-in 1
"""
        elif 'spine' in fabric_topology[switch_ip]["role"]:
            neighbor_command = neighbor_command + """
  address-family ipv4 unicast
    disable-peer-as-check
  address-family ipv6 unicast
    disable-peer-as-check
"""
        else:
            print(f"ERROR: Unexpected switch role for {fabric_topology[switch_ip]}")
            sys.exit()

        neighbor = fabric_topology[intf_attr["meta"]["neighbor_address"]]["hostname"]
        intf_command = intf_command + f"""
interface {intf}
 ip address {intf_attr["meta"]["p2p_ipv4_address"]}/{intf_attr["meta"]["p2p_ipv4_subnet"]}
"""
    # Todo: desription gives error because it doesn't respect ;
    #description connected-to-{neighbor}-{intf_attr["meta"]["neighbor_intf"]}

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
        print(f"INFO: Switch: {switch_ip} ({fabric_topology[switch_ip]['hostname']}): Trying to apply config by {switchuser}...")
        nxos_utils.run_cmd(args, commands, host_os, switch_ip, switchuser)
    except Exception as exc:
        print(f"Failed to apply configuration: {exc}")
        return

def remove_config(args, host_os, switch_ip, switchuser, fabric_topology):

    global_commands = """
conf
no feature bgp
no route-map redis-map
"""

    loopback_commands = """
no interface loopback0
"""

    intf_dict = fabric_topology[switch_ip]["intf"]
    intf_command = ''
    for intf, intf_attr in intf_dict.items():
        if 'mgmt' in intf:
            continue
        if 'switch' not in intf_attr["meta"]["neighbor_type"]:
            continue
        intf_command = intf_command + f"""
interface {intf}
 no ip address
 no description
"""

    commands = global_commands + intf_command + loopback_commands + '\nend\n'

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
        print(f"INFO: Switch: {switch_ip} ({fabric_topology[switch_ip]['hostname']}): Trying to remove config...")
        nxos_utils.run_cmd(args, commands, host_os, switch_ip, switchuser)
    except Exception as exc:
        print(f"Failed to remove configuration: {exc}")
        return

def change_config(args, host_os, switch_ip, switchuser, fabric_topology):
    start_t = time.time()
    if args.disable:
        remove_config(args, host_os, switch_ip, switchuser, fabric_topology)
    else:
        apply_config(args, host_os, switch_ip, switchuser, fabric_topology)
    print(f"INFO: Switch: {switch_ip} ({fabric_topology[switch_ip]['hostname']}): took {round((time.time() - start_t), 2)}s")


def main():
    args = parse_cmdline_arguments()
    host_os = nxos_utils.detect_host_os(args)
    if host_os != 'linux':
        print("ERROR: Must run remotely from a system with access to all switches in the fabric")
        return
    switch_dict = {}
    nxos_utils.get_switches(args, switch_dict)

    if args.ebgp_v4p2p:
        # Discover the fabric using seed switch from the provided switch-file
        fabric_topology = nxos_utils.get_fabric_topology(args, host_os, switch_dict)
        assign_bgp_params(args, fabric_topology)
        time_str = time.strftime("%Y-%m-%d-%H-%M-%S")
        output_filename = "nxos_fabric_topology_" + time_str + ".json"
        with open(output_filename, 'w') as f:
            json.dump(fabric_topology, f, indent=2)
        nxos_utils.common_worker(args, change_config, host_os, switch_dict, fabric_topology)

if __name__ == "__main__":
    main()
