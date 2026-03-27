#!/isan/bin/python3
"""Applies or removes config on Cisco NX-OS for RoCEv2 traffic.
Must run directly on the switch, like
n9k# python3 bootflash:roce_enable.py"""

__author__ = "Paresh Gupta"
__version__ = "0.20"
__updated__ = "27-Mar-2026-7-PM-PDT"

from cli import *
import sys
import argparse
from collections import OrderedDict

user_args = {}

def parse_cmdline_arguments():
    desc_str = \
        'Apply NX-OS Modular Quality of Service (QoS) Command-Line Interface\n' + \
        '(CLI) (MQC) for handling RoCEv2 traffic. This is just a wrapper \n' + \
        'for applying PFC and ECN config. It covers the most common \n' + \
        'scenario, but not all the cases. For customization, change this\n' + \
        'script, use following options, or use the NX-OS CLI directly\n' + \
        'V:' + __version__ + ' (' + __updated__ + ')'

    parser = argparse.ArgumentParser(description=desc_str,
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--pfc_cos', type=str, default=3, help='List of \
            class-of-service values for Pause frame, used by the command \
            pause pfc-cos <>. Default: 3')
    parser.add_argument('--cnp_dscp', type=str, default=48, help='List of \
            DSCP values for identifying CNP and assigning to priority queue.\
            Default: 48')
    parser.add_argument('--roce_dscp', type=str, default='24-31', help='List of\
            DSCP values for identifying RoCE traffic and assigning to no-drop \
            queue. Default: 24-31')
    parser.add_argument('--intf', type=str, default='', help='Interfaces \
            to be applied with RoCE and CNP classification policy. Must be \
            in NX-OS interface range format. Default: all Eth interfaces')
    parser.add_argument('-print_intf', default=False, action='store_true',
            help='Print all interface range. Do not apply config')
    parser.add_argument('-disable', default=False, action='store_true',
            help='Remove config appied by this utility')
    parser.add_argument('-print_only', default=False, action='store_true',
            help='Only print the config. Do not apply')

    args = parser.parse_args()
    user_args['pfc_cos'] = args.pfc_cos
    user_args['cnp_dscp'] = args.cnp_dscp
    user_args['roce_dscp'] = args.roce_dscp
    user_args['intf'] = args.intf
    user_args['print_intf'] = args.print_intf
    user_args['disable'] = args.disable
    user_args['print_only'] = args.print_only

def intf_range_str():
    command = """sh int bri | begin ignore-case Interface | cut -d ' ' -f 1 | inc ignore-case Eth"""
    output = ''

    try:
        output = cli(command)
    except Exception as e:
        print(f"Failed to run show interface command: {e}")

    grouped = OrderedDict()

    for line in output.strip().splitlines():
        parts = line.strip().split("/")
        key = "/".join(parts[:2])      # before second '/'
        last = parts[-1]               # after last '/'
        if key not in grouped:
            grouped[key] = line.strip()  # keep first full value (e.g., Eth1/1/1)
        else:
            grouped[key] += f"-{last}"   # append only last segment

    result = ",".join(grouped.values())
    if user_args['print_intf']:
        print(result)
        sys.exit()
    return(result)

def apply_config():
    qos_commands = f"""conf
        priority-flow-control watch-dog-interval on
        policy-map type network-qos qos_network
          class type network-qos c-8q-nq3
            mtu 9216
            pause pfc-cos {user_args['pfc_cos']}
          class type network-qos c-8q-nq-default
            mtu 9216
            exit
          exit
          class-map type qos match-any CNP
            match dscp {user_args['cnp_dscp']}
          class-map type qos match-any ROCEv2
            match dscp {user_args['roce_dscp']}
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

    intf_range = intf_range_str()
    intf_commands = f"""interface {intf_range}
        priority-flow-control mode on
        priority-flow-control watch-dog-interval on
        mtu 9216
        service-policy type qos input QOS_CLASSIFICATION
        no shutdown
        end"""

    commands = qos_commands + intf_commands

    if user_args['print_only']:
        print(commands)
        print('\n' + commands.replace('\n', ' ; '))
    else:
        try:
            cli(commands.replace('\n', ' ; '))
            print(f"Successfully applied configuration")
        except Exception as e:
            print(f"Failed to apply configuration: {e}")

def remove_config():
    intf_range = intf_range_str()
    intf_commands = f"""conf
        interface {intf_range}
        no priority-flow-control mode on
        no priority-flow-control watch-dog-interval on
        no service-policy type qos input QOS_CLASSIFICATION
        exit
        """

    qos_commands = f"""no priority-flow-control watch-dog-interval on
        system qos
        """

    sys_qos_queuing = "service-policy type queuing output QOS_EGRESS_PORT"
    c_queuing = 'sh run | section "system qos" | inc "' + sys_qos_queuing + '"'
    if cli(c_queuing).strip() == sys_qos_queuing:
        qos_commands = qos_commands + 'no ' + sys_qos_queuing + '\n'
    sys_qos_network = "service-policy type network-qos qos_network"
    c_network = 'sh run | section "system qos" | inc "' + sys_qos_network + '"'
    if cli(c_network).strip() == sys_qos_network:
        qos_commands = qos_commands + 'no ' + sys_qos_network + '\n'
    qos_commands = qos_commands + \
        f"""exit
        no policy-map type queuing QOS_EGRESS_PORT
        no policy-map type network-qos qos_network
        no policy-map type qos QOS_CLASSIFICATION
        no class-map type qos match-any CNP
        no class-map type qos match-any ROCEv2
        end"""

    commands = intf_commands + qos_commands

    if user_args['print_only']:
        print(commands)
        print('\n' + commands.replace('\n', ' ; '))
    else:
        try:
            cli(commands.replace('\n', ' ; '))
            print(f"Successfully removed configuration")
        except Exception as e:
            print(f"Failed to removed configuration: {e}")

if __name__ == "__main__":
    parse_cmdline_arguments()
    if user_args['disable']:
        remove_config()
    else:
        apply_config()
