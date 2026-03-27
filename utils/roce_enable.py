#!/usr/bin/env python3
"""Applies or removes config on Cisco NX-OS for RoCEv2 traffic.
Must run directly on the switch, like:
n9k# python3 bootflash:roce_enable.py
"""

__author__ = "Paresh Gupta"
__version__ = "0.22"
__updated__ = "27-Mar-2026-8-PM-PDT"

import sys
import argparse
from collections import OrderedDict
from cli import cli


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

    return parser.parse_args()


def normalize_cli_blob(blob):
    """Normalize multiline CLI text into single-line ' ; ' separated commands."""
    lines = [line.strip() for line in blob.splitlines() if line.strip()]
    return " ; ".join(lines)


def run_or_print(commands_blob, print_only=False):
    """Print or execute CLI commands."""
    pretty = commands_blob.strip()
    compact = normalize_cli_blob(commands_blob)

    if print_only:
        print(pretty)
        print("\n" + compact)
        return

    try:
        cli(compact)
    except Exception as exc:
        raise RuntimeError(f"CLI execution failed: {exc}") from exc


def build_interface_range(args):
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

    command = (
        "sh int bri | begin ignore-case Interface | "
        "cut -d ' ' -f 1 | inc ignore-case Eth"
    )

    try:
        output = cli(command)
    except Exception as exc:
        print(f"Failed to run show interface command: {exc}")
        sys.exit(1)

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


def apply_config(args):
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

    intf_range = build_interface_range(args)
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

    try:
        run_or_print(commands, print_only=args.print_only)
        if not args.print_only:
            print("Successfully applied configuration")
    except Exception as exc:
        print(f"Failed to apply configuration: {exc}")
        sys.exit(1)


def remove_config(args):
    intf_range = build_interface_range(args)
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
        if cli(c_queuing).strip() == sys_qos_queuing:
            qos_commands += f"no {sys_qos_queuing}\n"
    except Exception:
        pass

    sys_qos_network = "service-policy type network-qos qos_network"
    c_network = f'sh run | section "system qos" | inc "{sys_qos_network}"'
    try:
        if cli(c_network).strip() == sys_qos_network:
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

    try:
        run_or_print(commands, print_only=args.print_only)
        if not args.print_only:
            print("Successfully removed configuration")
    except Exception as exc:
        print(f"Failed to remove configuration: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    ARGS = parse_cmdline_arguments()
    if ARGS.disable:
        remove_config(ARGS)
    else:
        apply_config(ARGS)
