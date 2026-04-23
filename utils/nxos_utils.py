#!/usr/bin/env python3
"""Applies or removes config on Cisco NX-OS for RoCEv2 traffic.
Can run directly on the switch, like:
n9k# python3 bootflash:roce_enable.py
Or remotely on a Linux machine, like:
python3 roce_enable.py --switch-file nexus_switches.txt
"""

__author__ = "Paresh Gupta"
__version__ = "0.31"
__updated__ = "12-Apr-2026-1-PM-PDT"

import sys
import subprocess
import re
import time
import json
import argparse
import concurrent.futures
from collections import deque
from collections import defaultdict

# Constants for Regex Patterns
CDP_IP_REGEX = re.compile(r"address:\s*(\d{1,3}(?:\.\d{1,3}){3})", re.IGNORECASE)
LLDP_IP_REGEX = re.compile(r"Address:\s*(\d{1,3}(?:\.\d{1,3}){3})", re.IGNORECASE)
HOSTNAME_REGEX = re.compile(r"System Name:\s*(\S+)|Device ID:\s*(\S+)", re.IGNORECASE)

def get_base_parser():
    base_parser = argparse.ArgumentParser(add_help=False)

    base_parser.add_argument(
        "--switch-file",
        type=str,
        default="",
        help=(
            "File containing list of switches in format: IP,user,password,..."
            "Mandatory when running remotely from Linux machine"
        ),
    )
    base_parser.add_argument(
        "--intf",
        type=str,
        default="",
        help=(
            "Interfaces to be modified. "
            "Must be in NX-OS interface range format. "
            "Default: all Eth interfaces."
        ),
    )
    base_parser.add_argument(
        "--print-intf",
        default=False,
        action="store_true",
        help="Print all interface range. Do not apply config.",
    )
    base_parser.add_argument(
        "--disable",
        default=False,
        action="store_true",
        help="Remove config applied by this utility.",
    )
    base_parser.add_argument(
        "--print-only",
        default=False,
        action="store_true",
        help="Only print the config. Do not apply.",
    )
    base_parser.add_argument(
        "--host",
        choices=["auto", "nxos", "linux"],
        default="auto",
        help="Execution host: auto-detect (default), force nxos, or force linux.",
    )
    base_parser.add_argument(
        "--fabric",
        default=False,
        action="store_true",
        help="Use the seed switch from the provided switch-file to discover "
        "all switches in the fabric and then make change on all the switches. "
        "The other approach would be to provide all switches in the switch-file"
        " without this option set",
    )
    base_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="Verbose logs"
    )
    base_parser.add_argument(
        "-vv",
        "--more_verbose",
        action="store_true",
        default=False,
        help="More verbose logs"
    )
    base_parser.add_argument(
        "-vvv",
        "--most_verbose",
        action="store_true",
        default=False,
        help="Most verbose logs with raw dump"
    )
    return base_parser


def get_switches(args, switch_dict):
    """
    Parse the input-file

    The format of the file is expected to carry:
    IP_Address,username,password,description
    Only one entry is expected per line
    Line with prefix # is ignored
    """

    with open(args.switch_file, 'r') as f:
        for line in f:
            if not line.startswith('#'):
                line = line.strip()
                if line.startswith('['):
                    if not line.endswith(']'):
                        print('Input file format error. Line starts' \
                              ' with [ but does not end with ]. File:' + \
                              args.switch_file + '. Line:' + line)
                        sys.exit()
                    line = line.replace('[', '')
                    line = line.replace(']', '')
                    line = line.strip()
                    continue

                sw = line.split(',')
                if len(sw) < 3:
                    print('ERROR: Line not in correct input format:'
                    'IP_Address,username,password')
                    continue
                switch_dict[sw[0]] = {}
                switch_dscr = sw[3] if len(sw) == 4 else ''
                switch_dict[sw[0]]['meta'] = [sw[1], sw[2], switch_dscr]

    if not switch_dict:
        print('ERROR: No switches found. Check input file.')
        sys.exit()

def detect_host_os(args):
    """Detect NX-OS vs Linux."""
    if args.host == "nxos":
        import cli
        return "nxos"
    if args.host == "linux":
        if args.switch_file == '':
            print("ERROR: A file with a list of switches is mandatory when running remotely")
            sys.exit(1)
        return "linux"

    # auto
    try:
        import cli
        cli.cli("show clock")
        return "nxos"
    except Exception:
        if args.switch_file == '':
            print("ERROR: A file with a list of switches is mandatory when running remotely")
            sys.exit(1)
        return "linux"


def normalize_cli_blob(blob):
    """Normalize multiline CLI text into single-line ' ; ' separated commands."""
    lines = [line.strip() for line in blob.splitlines() if line.strip()]
    return " ; ".join(lines)

def run_cmd(args, nxos_cmd, host_os, switch_ip, switchuser):
    """Execute CLI commands."""
    compact = normalize_cli_blob(nxos_cmd)
    ret = None

    if host_os == 'nxos':
        try:
            import cli
            ret = cli.cli(compact)
        except Exception as exc:
            raise RuntimeError(f"CLI -{nxos_cmd}- execution failed: {exc}")
    if host_os == 'linux':
        cmd = 'ssh ' + switchuser + '@' + switch_ip + ' -o BatchMode=yes ' + \
              '-o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new'
        cmd_list = cmd.split(' ')
        cmd_list.append(compact)
        try:
            output = subprocess.run(cmd_list, stdout=subprocess.PIPE, \
                        stderr=subprocess.PIPE, timeout=60)
            if output.returncode != 0:
                print(f"Error: Switch {switch_ip}: failed to run \n{nxos_cmd}")
                print(f"{str(output.stderr.decode('utf-8').strip())}")
                print(f"{str(output.stdout.decode('utf-8').strip())}")
            else:
                if args.most_verbose:
                    print(f"Switch {switch_ip}: for command {nxos_cmd} returned")
                    print(output.stdout.decode('utf-8'))
                ret = str(output.stdout.decode('utf-8').strip())
        except Exception as e:
            raise Exception(e)
    return ret

def parse_interface_name(if_name):
    """
    Parse interface name into prefix and list of numeric components.
    Examples:
    - eth1/1 -> prefix='eth', nums=[1,1]
    - eth1/1/1 -> prefix='eth', nums=[1,1,1]
    """
    match = re.match(r"([a-zA-Z]+)([\d/]+)", if_name)
    if not match:
        return None, []
    prefix = match.group(1)
    nums = list(map(int, match.group(2).split('/')))
    return prefix, nums

def generate_ranges(nums_list):
    """
    Given a sorted list of integers, generate ranges as tuples (start, end).
    Consecutive numbers are grouped into ranges.
    """
    ranges = []
    start = prev = nums_list[0]
    for num in nums_list[1:]:
        if num == prev + 1:
            prev = num
        else:
            ranges.append((start, prev))
            start = prev = num
    ranges.append((start, prev))
    return ranges

def format_range(prefix, base_nums, ranges):
    """
    Format the interface range string based on prefix, base numbers, and ranges.
    base_nums: list of numbers except the last component
    ranges: list of (start, end) tuples for the last component
    """
    base_str = '/'.join(str(n) for n in base_nums)
    parts = []
    for start, end in ranges:
        if start == end:
            parts.append(f"{prefix}{base_str}/{start}")
        else:
            parts.append(f"{prefix}{base_str}/{start}-{end}")
    return ','.join(parts)

def build_interface_range(args, host_os, switch_ip, switchuser):
    """
    Generate interface range string from a list of interface names.
    Supports interfaces with 2 or 3 numeric components.
    """

    nxos_cmd = "sh int bri | begin ignore-case Interface | " + \
        "cut -d ' ' -f 1 | inc ignore-case Eth"

    output = run_cmd(args, nxos_cmd, host_os, switch_ip, switchuser)
    if output is None:
        print('Error: ' + nxos_cmd)
        sys.exit()
    interface_list = output.split('\n')

    # Group interfaces by prefix and base numbers (all but last number)
    groups = defaultdict(list)
    for if_name in interface_list:
        prefix, nums = parse_interface_name(if_name)
        if prefix is None or not nums:
            continue
        base = tuple(nums[:-1])  # all but last number
        last_num = nums[-1]
        groups[(prefix, base)].append(last_num)

    # For each group, generate ranges and format
    range_strings = []
    for (prefix, base), last_nums in groups.items():
        sorted_nums = sorted(last_nums)
        ranges = generate_ranges(sorted_nums)
        range_str = format_range(prefix, list(base), ranges)
        range_strings.append(range_str)

    return ','.join(range_strings)

def parse_cdp_neighbors(cdp_out):
    """Parse CLI output to extract neighbor IP addresses."""
    neighbors = set()
    sub_section = ''.join(re.findall(r'Mgmt address.*\n.*\n', cdp_out, re.IGNORECASE))
    matches = re.findall(r'IPv4 Address:[ ]{1,}(.*)', sub_section, re.IGNORECASE)

    return set(matches)
    '''
    matches = ip_regex.findall(output)
    for match in matches:
        neighbors.add(match)
    return neighbors
    '''

def parse_lldp_neighbors(lldp_out):
    """Parse CLI output to extract neighbor IP addresses."""
    neighbors = set()
    matches = re.findall(r'Management Address:[ ]{1,}(.*)', lldp_out, re.IGNORECASE)
    return set(matches)

def populate_intf_dict(args, intf_dict, cdp_out, lldp_out):
    # Populate intf dictionary with neighbor information
    for section in cdp_out.split('-------------------------------------'):
        if 'Device' not in section:
            continue
        neighbor_name = ''.join(re.findall(r'Name:[ ]{1,}(.*)',
                            section, re.IGNORECASE))
        if neighbor_name == '':
            neighbor_name = ''.join(re.findall(r'Device ID:[ ]{1,}(.*)',
                            section, re.IGNORECASE))
        sub_section = ''.join(re.findall(r'Mgmt address.*\n.*\n',
                            section, re.IGNORECASE))
        neighbor_address = ''.join(re.findall(r'IPv4 Address:[ ]{1,}(.*)',
                            sub_section, re.IGNORECASE))
        local_intf = ''.join(re.findall(r'Interface:[ ]{1,}(.*),',
                            section, re.IGNORECASE))
        neighbor_intf = ''.join(re.findall(r'Port ID .*:[ ]{1,}(.*)',
                            section, re.IGNORECASE))

        neighbor_type = 'other'
        if re.search('Switch', section, re.IGNORECASE):
            neighbor_type = 'switch'

        if local_intf not in intf_dict:
            intf_dict[local_intf] = {}
        per_intf_dict = intf_dict[local_intf]

        if 'meta' not in per_intf_dict:
            per_intf_dict['meta'] = {}
        meta_dict = per_intf_dict['meta']
        meta_dict['neighbor_name'] = neighbor_name
        meta_dict['neighbor_address'] = neighbor_address
        meta_dict['neighbor_intf'] = neighbor_intf
        meta_dict['neighbor_type'] = neighbor_type

        if args.verbose:
            print(f"CDP: {local_intf}, {neighbor_name}, {neighbor_address}, {neighbor_intf}, {neighbor_type}")
    # now allow LLDP to take precedence over CDP
    for section in lldp_out.split('\n\n'):
        if 'Chassis' not in section:
            continue
        neighbor_name = ''.join(re.findall(r'System Name:[ ]{1,}(.*)',
                                section, re.IGNORECASE))
        neighbor_address = ''.join(re.findall(r'Management Address:[ ]{1,}(.*)',
                                section, re.IGNORECASE))
        local_intf = ''.join(re.findall(r'Local Port id:[ ]{1,}(.*)',
                                section, re.IGNORECASE))
        # When docker is installed, Ubuntu's encap becomes router(R)
        # instead of station(S). So check for Linux 
        if re.search('tation', section, re.IGNORECASE) or \
            re.search('Linux', section, re.IGNORECASE):
            neighbor_type = 'host'
            # portDesc is "Interface  28 as enp154s0d22" by lldpad
            # portDesc can be 'Interface  18 as enp77s0d8' or 'fabric0'
            # Assumption: interface name is the last word
            portDesc = ''.join(re.findall(r'Port Description:[ ]{1,}(.*)',
                                section, re.IGNORECASE))
            neighbor_intf = portDesc.strip().split()[-1]
        elif re.search('cisco', section, re.IGNORECASE):
            neighbor_type = 'switch'
            neighbor_intf = ''.join(re.findall(r'\nPort id:[ ]{1,}(.*)',
                                section, re.IGNORECASE))
        else:
            neighbor_type = 'other'
            neighbor_intf = 'unknown'

        if local_intf not in intf_dict:
            intf_dict[local_intf] = {}
        per_intf_dict = intf_dict[local_intf]

        if 'meta' not in per_intf_dict:
            per_intf_dict['meta'] = {}
        meta_dict = per_intf_dict['meta']
        meta_dict['neighbor_name'] = neighbor_name
        meta_dict['neighbor_address'] = neighbor_address
        meta_dict['neighbor_intf'] = neighbor_intf
        meta_dict['neighbor_type'] = neighbor_type
        if args.verbose:
            print(f"LLDP: {local_intf}, {neighbor_name}, {neighbor_address}, {neighbor_intf}, {neighbor_type}")

def discover_fabric(args, host_os, switch_ip, switchuser, max_depth=5):
    """Discover the NX-OS fabric using BFS."""
    visited = set()
    queue = deque([(switch_ip, 0)]) # Tuple of (IP, current_depth)
    topology = {}

    print(f"--- Starting Fabric Discovery from Seed: {switch_ip} ---")

    while queue:
        current_ip, depth = queue.popleft()

        if current_ip in visited:
            continue
        if depth > max_depth:
            print(f"Reached max depth ({max_depth}) at {current_ip}. Skipping.")
            continue

        visited.add(current_ip)
        print(f"[{depth}/{max_depth}] Discovering {current_ip}...")

        # Get Hostname
        output = run_cmd(args, 'show hostname', host_os, current_ip, switchuser)
        if output is None:
            print(f"Error: Unable to get hostname from {current_ip}")
            continue
        hostname = output

        # Run CDP and LLDP commands
        cdp_cmd = "show cdp neighbors detail"
        cdp_out = run_cmd(args, cdp_cmd, host_os, current_ip, switchuser)
        if cdp_out is None:
            print(f"Error: Unable to get {cdp_cmd} from {switch_ip}")
            continue

        lldp_cmd = "show lldp neighbors detail"
        lldp_out = run_cmd(args, lldp_cmd, host_os, current_ip, switchuser)
        if lldp_out is None:
            print(f"Error: Unable to get {lldp_cmd} from {switch_ip}")
            continue

        # Extract neighbor IPs
        cdp_neighbors = parse_cdp_neighbors(cdp_out)
        lldp_neighbors = parse_lldp_neighbors(lldp_out)

        # Combine unique neighbors
        all_neighbors = list(cdp_neighbors.union(lldp_neighbors))

        # Remove self-IP if it somehow shows up
        if current_ip in all_neighbors:
            all_neighbors.remove(current_ip)

        # Record in topology
        topology[current_ip] = {
            "hostname": hostname,
            "depth": depth,
            "neighbors": all_neighbors,
            "intf": {},
            "status": "success"
        }
        # Populate intf dictionary with neighbor information
        intf_dict = topology[current_ip]["intf"]
        populate_intf_dict(args, intf_dict, cdp_out, lldp_out)

        for intf, intf_attr in intf_dict.items():
            if 'switch' not in intf_attr["meta"]["neighbor_type"]:
                neighbor_ip = intf_attr["meta"]["neighbor_address"]
                all_neighbors.remove(neighbor_ip)

        # Add new neighbors to the queue
        for neighbor_ip in all_neighbors:
            if neighbor_ip not in visited:
                queue.append((neighbor_ip, depth + 1))

    return topology

def discover_fabric_topology(args, host_os, switch_ip, switchuser):
    fabric_topology = discover_fabric(args, host_os, switch_ip, switchuser, max_depth=5)
    time_str = time.strftime("%Y-%m-%d-%H-%M-%S")
    output_filename = "nxos_fabric_topology_" + time_str + ".json"
    print(f"--- Discovery Complete ---")

    with open(output_filename, 'w') as f:
        json.dump(fabric_topology, f, indent=2)

    if args.more_verbose:
        print(f"{output_filename}:")
        print(json.dumps(fabric_topology, indent=2))

    return fabric_topology

def get_fabric_topology(args, host_os, switch_dict):
    fabric_topology = {}
    discovered_switch_dict = {}
    for switch_ip, switch_attr in switch_dict.items():
        switchuser = switch_attr['meta'][0]
        fabric_topology = discover_fabric_topology(args, \
                                host_os, switch_ip, switchuser)

    for switch_ip, switch_attr in fabric_topology.items():
        discovered_switch_dict[switch_ip] = {}
        # use the same user as seed switch, blank passwd
        discovered_switch_dict[switch_ip]['meta'] = [switchuser, '', '']
        #                                              switch_attr['hostname']]
    switch_dict.update(discovered_switch_dict)

    return fabric_topology

def common_worker(args, worker, worker_arg):
    host_os = detect_host_os(args)

    start_t = time.time()
    print('--------------------------------------------------------------------------------')
    if host_os == 'linux':
        switch_dict = {}
        get_switches(args, switch_dict)

        # Discover the fabric using seed switch from the provided switch-file
        if args.fabric:
            fabric_topology = get_fabric_topology(args, host_os, switch_dict)

        '''
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(switch_dict)) as e:
            futures = {}
            for switch_ip, switch_attr in switch_dict.items():
                switchuser = switch_attr['meta'][0]
                switchpassword = switch_attr['meta'][1]
                print(f"INFO: Switch: {switch_ip} ({switch_attr['meta'][2]}): Starting to work...")
                future = e.submit(worker, args, host_os, switch_ip, switchuser, fabric_topology[switch_ip])
                futures[future] = switch_ip

            for future in concurrent.futures.as_completed(futures):
                switch_ip = futures[future]
                try:
                    result = future.result()  # This will raise any exception that occurred
                    print(f"INFO: Switch: {switch_ip}: Completed successfully")
                except Exception as exc:
                    print(f"Switch: {switch_ip}: Generated an exception: {exc}")
        '''

        # Following is a non-multithreaded way
        for switch_ip, switch_attr in switch_dict.items():
            switchuser = switch_attr['meta'][0]
            print(f"INFO: Switch: {switch_ip} ({switch_attr['meta'][2]}): Starting to work...")
            worker(args, host_os, switch_ip, switchuser, fabric_topology, worker_arg)

    elif host_os == 'nxos':
        worker(args, host_os, 'local', None)
    else:
        print("ERROR: Unknown host OS")
    print('--------------------------------------------------------------------------------')
    print(f"INFO: Total time: {round((time.time() - start_t), 2)}s")


def main():
    pass

if __name__ == "__main__":
    main()
