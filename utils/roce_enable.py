#!/usr/bin/env python3
"""Applies or removes config on Cisco NX-OS for RoCEv2 traffic.
Can run directly on the switch, like:
n9k# python3 bootflash:roce_enable.py
Or remotely on a Linux machine, like:
python3 roce_enable.py --switch-file nexus_switches.txt
"""

__author__ = "Paresh Gupta"
__version__ = "0.30"
__updated__ = "27-Mar-2026-8-PM-PDT"

import sys
import argparse
from collections import OrderedDict
import subprocess

def parse_cmdline_arguments():
    desc_str = (
        "Apply NX-OS Modular Quality of Service (QoS) Command-Line Interface\n"
        "(CLI) (MQC) for handling RoCEv2 traffic. This is a wrapper\n"
        "for applying PFC and ECN config. It covers the most common\n"
        "scenario, but not all cases. For customization, change this\n"
        "script, use the options below, or use NX-OS CLI directly.\n"
        f"V:{__version__} ({__updated__})"
    )

    parser = argparse.ArgumentParser(
        description=desc_str,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--pfc-cos",
        dest="pfc_cos",
        type=str,
        default="3",
        help=(
            "List of class-of-service values for Pause frame, used by "
            "'pause pfc-cos <...>'. Default: 3"
        ),
    )
    parser.add_argument(
        "--cnp-dscp",
        dest="cnp_dscp",
        type=str,
        default="48",
        help=(
            "List of DSCP values for identifying CNP and assigning to "
            "priority queue. Default: 48"
        ),
    )
    parser.add_argument(
        "--roce-dscp",
        dest="roce_dscp",
        type=str,
        default="24-31",
        help=(
            "List of DSCP values for identifying RoCE traffic and assigning "
            "to no-drop queue. Default: 24-31"
        ),
    )
    parser.add_argument(
        "--intf",
        type=str,
        default="",
        help=(
            "Interfaces to be applied with RoCE and CNP classification policy. "
            "Must be in NX-OS interface range format. "
            "Default: all Eth interfaces."
        ),
    )
    parser.add_argument(
        "--print-intf",
        default=False,
        action="store_true",
        help="Print all interface range. Do not apply config.",
    )
    parser.add_argument(
        "--disable",
        default=False,
        action="store_true",
        help="Remove config applied by this utility.",
    )
    parser.add_argument(
        "--print-only",
        default=False,
        action="store_true",
        help="Only print the config. Do not apply.",
    )
    parser.add_argument(
        "--host",
        choices=["auto", "nxos", "linux"],
        default="auto",
        help="Execution host: auto-detect (default), force nxos, or force linux.",
    )
    parser.add_argument(
        "--switch-file",
        type=str,
        default="",
        help=(
            "File containing list of switches in format: IP,user,password,..."
            "Mandatory when running remotely from Linux machine"
        ),
    )

    return parser.parse_args()

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
                    print(f'ERROR: Line not in correct input format:'
                    'IP_Address,username,password')
                    continue
                switch_dict[sw[0]] = {}
                switch_dscr = sw[3] if len(sw) == 4 else ''
                switch_dict[sw[0]]['meta'] = [sw[1], sw[2], switch_dscr]

    if not switch_dict:
        print('ERROR: No switches found. Check input file.')
        sys.exit()

def detect_host_os(host):
    """Detect NX-OS vs Linux."""
    if host == "nxos":
        import cli
        return "nxos"
    if host == "linux":
        return "linux"

    # auto
    try:
        import cli
        cli.cli("show clock")
        return "nxos"
    except Exception:
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
                print(nxos_cmd + ' failed on ' + switch_ip + ':' + \
                        str(output.stderr.decode('utf-8').strip()))
            else:
                ret = str(output.stdout.decode('utf-8').strip())
        except Exception as e:
            raise Exception(e)
    return ret

def build_interface_range(args, host_os, switch_ip, switchuser):
    """Return interface range string in grouped form:
    Eth1/1/1-2,Eth1/2/1-2,...
    """
    # If user provided --intf, honor it directly.
    if args.intf.strip():
        result = args.intf.strip()
        if args.print_intf:
            print(result)
            sys.exit(0)
        return result

    nxos_cmd = "sh int bri | begin ignore-case Interface | " + \
        "cut -d ' ' -f 1 | inc ignore-case Eth"

    output = run_cmd(args, nxos_cmd, host_os, switch_ip, switchuser)
    if output is None:
        print('Error: ' + nxos_cmd)
        sys.exit()

    grouped = OrderedDict()

    for raw in output.strip().splitlines():
        line = raw.strip()
        if not line or "/" not in line:
            continue
        if not line.lower().startswith("eth"):
            continue

        parts = line.split("/")
        # Expect at least EthX/Y/Z style.
        if len(parts) < 3:
            # Keep as-is for safety if format is shorter than expected.
            grouped.setdefault(line, line)
            continue

        key = "/".join(parts[:2])  # before second '/'
        last = parts[-1]           # after last '/'

        if key not in grouped:
            grouped[key] = line
        else:
            grouped[key] += f"-{last}"

    result = ",".join(grouped.values())

    if not result:
        print("No Ethernet interfaces discovered. Exiting.")
        sys.exit(1)

    if args.print_intf:
        print(result)
        sys.exit(0)

    return result


def apply_config(args, host_os, switch_ip, switchuser):
    qos_commands = f"""
conf
priority-flow-control watch-dog-interval on
policy-map type network-qos qos_network
  class type network-qos c-8q-nq3
    mtu 9216
    pause pfc-cos {args.pfc_cos}
  class type network-qos c-8q-nq-default
    mtu 9216
    exit
  exit
class-map type qos match-any CNP
  match dscp {args.cnp_dscp}
class-map type qos match-any ROCEv2
  match dscp {args.roce_dscp}
policy-map type qos QOS_CLASSIFICATION
  class ROCEv2
    set qos-group 3
  class CNP
    set qos-group 7
  class class-default
    set qos-group 0
    exit
  exit
policy-map type queuing QOS_EGRESS_PORT
  class type queuing c-out-8q-q6
    bandwidth remaining percent 0
  class type queuing c-out-8q-q5
    bandwidth remaining percent 0
  class type queuing c-out-8q-q4
    bandwidth remaining percent 0
  class type queuing c-out-8q-q3
    bandwidth remaining percent 50
    random-detect minimum-threshold 950 kbytes maximum-threshold 3000 kbytes drop-probability 7 weight 0 ecn
  class type queuing c-out-8q-q2
    bandwidth remaining percent 0
  class type queuing c-out-8q-q1
    bandwidth remaining percent 0
  class type queuing c-out-8q-q-default
    bandwidth remaining percent 50
  class type queuing c-out-8q-q7
    priority level 1
system qos
  service-policy type queuing output QOS_EGRESS_PORT
  service-policy type network-qos qos_network
"""

    intf_range = build_interface_range(args, host_os, switch_ip, switchuser)
    intf_commands = f"""
interface {intf_range}
priority-flow-control mode on
priority-flow-control watch-dog-interval on
mtu 9216
service-policy type qos input QOS_CLASSIFICATION
no shutdown
end
"""

    commands = qos_commands + "\n" + intf_commands

    if args.print_only:
        print(commands.strip())
        print("\n" + normalize_cli_blob(commands))
        return

    try:
        run_cmd(args, commands, host_os, switch_ip, switchuser)
        print("Successfully applied configuration")
    except Exception as exc:
        print(f"Failed to apply configuration: {exc}")
        sys.exit(1)

def remove_config(args, host_os, switch_ip, switchuser):
    intf_range = build_interface_range(args, host_os, switch_ip, switchuser)
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
    c_queuing = f'sh run | section "system qos" | inc "{sys_qos_queuing}"'
    try:
        if run_cmd(args, c_queuing, host_os, switch_ip, switchuser).strip() \
                == sys_qos_queuing:
            qos_commands += f"no {sys_qos_queuing}\n"
    except Exception:
        pass

    sys_qos_network = "service-policy type network-qos qos_network"
    c_network = f'sh run | section "system qos" | inc "{sys_qos_network}"'
    try:
        if run_cmd(args, c_network, host_os, switch_ip, switchuser).strip() \
                == sys_qos_network:
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

    commands = intf_commands + "\n" + qos_commands

    if args.print_only:
        print(commands.strip())
        print("\n" + normalize_cli_blob(commands))
        return

    try:
        run_cmd(args, commands, host_os, switch_ip, switchuser)
        print("Successfully removed configuration")
    except Exception as exc:
        print(f"Failed to remove configuration: {exc}")
        sys.exit(1)

def change_config(args, host_os, switch_ip, switchuser):
    if args.disable:
        remove_config(args, host_os, switch_ip, switchuser)
    else:
        apply_config(args, host_os, switch_ip, switchuser)

def main():
    args = parse_cmdline_arguments()
    host_os = detect_host_os(args.host)
    if host_os == 'linux' and args.switch_file == '':
        print(f"ERROR: A file with a list of switches is mandatory when running remotely")
        sys.exit(1)

    if host_os == 'linux':
        switch_dict = {}
        get_switches(args, switch_dict)
        for switch_ip, switch_attr in switch_dict.items():
            switchuser = switch_attr['meta'][0]
            switchpassword = switch_attr['meta'][1]
            print('----------------------------------------')
            print(f"INFO: Starting to work on the switch {switch_ip} ({switch_attr['meta'][2]})")
            change_config(args, host_os, switch_ip, switchuser)
            print(f"INFO: Done working on {switch_ip} ({switch_attr['meta'][2]})")
        print('----------------------------------------')
    elif host_os == 'nxos':
        change_config(args, host_os, None, None)
    else:
        print(f"ERROR: Unknown host OS")

if __name__ == "__main__":
    main()
