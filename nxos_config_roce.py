#!/usr/bin/env python3
"""Applies or removes config on Cisco NX-OS for RoCEv2 traffic.
Can run directly on the switch, like:
n9k# python3 bootflash:roce_enable.py
Or remotely on a Linux machine, like:
python3 roce_enable.py --switch-file nexus_switches.txt
"""

__author__ = "Paresh Gupta"
__version__ = "0.60"
__updated__ = "26-Apr-2026-1-PM-PDT"

import argparse
import time
from utils import nxos_utils

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
        "--fabric",
        default=False,
        action="store_true",
        help="Use the seed switch from the provided switch-file to discover "
        "all switches in the fabric and then make change on all the switches. "
        "The other approach would be to provide all switches in the switch-file"
        " without this option set",
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

    return parser.parse_args()

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
interface {intf_range}
priority-flow-control mode on
priority-flow-control watch-dog-interval on
mtu 9216
service-policy type qos input QOS_CLASSIFICATION
no shutdown
end
"""

    commands = qos_commands + intf_commands

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
        print(f"INFO: Switch: {switch_ip}: Trying to apply config...")
        nxos_utils.run_cmd(args, commands, host_os, switch_ip, switchuser)
    except Exception as exc:
        print(f"Failed to apply configuration: {exc}")
        return

def remove_config(args, host_os, switch_ip, switchuser):
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

def change_config(args, host_os, switch_ip, switchuser, x, xx):
    start_t = time.time()
    if args.disable:
        remove_config(args, host_os, switch_ip, switchuser)
    else:
        apply_config(args, host_os, switch_ip, switchuser)
    print(f"INFO: Switch: {switch_ip} took {round((time.time() - start_t), 2)}s")


def main():
    args = parse_cmdline_arguments()
    nxos_utils.common_worker(args, change_config, None)

if __name__ == "__main__":
    main()
