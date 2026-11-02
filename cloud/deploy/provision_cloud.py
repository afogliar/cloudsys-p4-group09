#!/usr/bin/env python3
"""Provision the Project 4 cloud VM on SWITCHengines (OpenStack).

Idempotent: re-running reuses the keypair, security group, server, floating IP,
and volume. It does not install software or start containers.

Usage:
  OS_CLIENT_CONFIG_FILE=/path/to/clouds.yaml \
    python3 provision_cloud.py --ssh-cidr 203.0.113.10/32
"""

import argparse
import ipaddress
import os
import pathlib
import sys

import openstack
from openstack.exceptions import ConflictException

SERVER_NAME = "p4-group09-cloud"
KEYPAIR_NAME = "p4-group09-cloud"
SG_NAME = "p4-cloud"
IMAGE_NAME = "Ubuntu Noble 24.04 (SWITCHengines)"
FLAVOR_NAME = "m1.large"
NETWORK_NAME = "private"
PUBLIC_NETWORK_NAME = "public"
VOLUME_NAME = "p4-group09-data"
VOLUME_TYPE = "ceph-standard"

args = None


def ensure_keypair(conn, pubkey_path):
    keypair = conn.compute.find_keypair(KEYPAIR_NAME)
    if keypair:
        print(f"keypair: reuse {KEYPAIR_NAME}")
        return keypair
    public_key = pathlib.Path(pubkey_path).read_text().strip()
    keypair = conn.compute.create_keypair(name=KEYPAIR_NAME, public_key=public_key)
    print(f"keypair: created {KEYPAIR_NAME}")
    return keypair


def rule_key(rule):
    return (
        rule.get("direction"),
        rule.get("ethertype"),
        rule.get("protocol"),
        rule.get("port_range_min"),
        rule.get("port_range_max"),
        rule.get("remote_ip_prefix"),
        rule.get("remote_group_id"),
    )


def ensure_security_group(conn, ssh_cidr):
    group = conn.network.find_security_group(SG_NAME)
    if group:
        print(f"security group: reuse {SG_NAME}")
    else:
        group = conn.network.create_security_group(
            name=SG_NAME,
            description="Project 4 cloud VM: SSH admin plus public HTTPS",
        )
        print(f"security group: created {SG_NAME}")

    wanted = [
        dict(direction="ingress", ethertype="IPv4", protocol="tcp",
             port_range_min=22, port_range_max=22, remote_ip_prefix=ssh_cidr),
        dict(direction="ingress", ethertype="IPv4", protocol="tcp",
             port_range_min=80, port_range_max=80, remote_ip_prefix="0.0.0.0/0"),
        dict(direction="ingress", ethertype="IPv4", protocol="tcp",
             port_range_min=443, port_range_max=443, remote_ip_prefix="0.0.0.0/0"),
        dict(direction="egress", ethertype="IPv4", remote_ip_prefix="0.0.0.0/0"),
        dict(direction="egress", ethertype="IPv6", remote_ip_prefix="::/0"),
    ]
    existing = {rule_key(rule) for rule in (group.security_group_rules or [])}
    for rule in wanted:
        if rule_key(rule) in existing:
            continue
        try:
            conn.network.create_security_group_rule(security_group_id=group.id, **rule)
            print(f"security group rule: added {rule}")
        except ConflictException:
            print(f"security group rule: already exists {rule}")

    return conn.network.find_security_group(SG_NAME)


def ensure_server(conn):
    server = conn.compute.find_server(SERVER_NAME)
    if server:
        print(f"server: reuse {SERVER_NAME} ({server.status})")
        return conn.compute.wait_for_server(server, status="ACTIVE", wait=600)

    image = conn.compute.find_image(IMAGE_NAME)
    flavor = conn.compute.find_flavor(FLAVOR_NAME)
    network = conn.network.find_network(NETWORK_NAME)
    if not image or not flavor or not network:
        sys.exit("missing image, flavor, or private network")

    print(f"server: creating {SERVER_NAME} ({FLAVOR_NAME}, {IMAGE_NAME})")
    server = conn.compute.create_server(
        name=SERVER_NAME,
        image_id=image.id,
        flavor_id=flavor.id,
        networks=[{"uuid": network.id}],
        key_name=KEYPAIR_NAME,
        security_groups=[{"name": SG_NAME}],
    )
    server = conn.compute.wait_for_server(server, status="ACTIVE", wait=900)
    print(f"server: active {server.id}")
    return server


def ensure_floating_ip(conn, server):
    ports = list(conn.network.ports(device_id=server.id))
    if not ports:
        sys.exit("server has no network port")
    server_port = next(
        (
            port
            for port in ports
            if any(
                fixed_ip.get("ip_address", "").startswith("10.")
                for fixed_ip in (port.fixed_ips or [])
            )
        ),
        ports[0],
    )

    floating_ips = list(conn.network.ips())

    for candidate in floating_ips:
        if candidate.port_id == server_port.id and candidate.floating_ip_address:
            print(f"floating ip: reuse {candidate.floating_ip_address}")
            return candidate

    floating_ip = next(
        (candidate for candidate in floating_ips if candidate.port_id is None),
        None,
    )
    if floating_ip:
        print(f"floating ip: reuse unassigned {floating_ip.floating_ip_address}")
    else:
        public_network = conn.network.find_network(PUBLIC_NETWORK_NAME)
        if not public_network:
            sys.exit("public network not found")
        floating_ip = conn.network.create_ip(floating_network_id=public_network.id)
        print(f"floating ip: created {floating_ip.floating_ip_address}")

    conn.network.update_ip(floating_ip, port_id=server_port.id)
    print("floating ip: associated with server port")
    return floating_ip


def ensure_volume(conn, server):
    volume = conn.block_storage.find_volume(VOLUME_NAME)
    if volume:
        print(f"volume: reuse {VOLUME_NAME} ({volume.size} GB, {volume.status})")
    else:
        volume = conn.block_storage.create_volume(
            name=VOLUME_NAME,
            size=args.volume_size,
            volume_type=VOLUME_TYPE,
            availability_zone="nova",
        )
        print(f"volume: creating {VOLUME_NAME} ({args.volume_size} GB)")
        volume = conn.block_storage.wait_for_status(
            volume, status="available", wait=600
        )

    attachments = list(conn.compute.volume_attachments(server))
    if not any(attachment.volume_id == volume.id for attachment in attachments):
        conn.compute.create_volume_attachment(server, volume_id=volume.id)
        print("volume: attached to server")
    else:
        print("volume: already attached")

    return volume


def main():
    global args
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cloud", default="engines")
    parser.add_argument(
        "--config-file",
        default=str(pathlib.Path.home() / ".config/openstack/clouds.yaml"),
    )
    parser.add_argument(
        "--pubkey",
        default=str(pathlib.Path.home() / ".ssh/p4_group09_cloud.pub"),
    )
    parser.add_argument("--ssh-cidr", required=True, help="CIDR allowed to SSH")
    parser.add_argument("--volume-size", type=int, default=50)
    args = parser.parse_args()

    try:
        ssh_network = ipaddress.ip_network(args.ssh_cidr, strict=False)
    except ValueError as exc:
        sys.exit(f"invalid --ssh-cidr: {exc}")
    if ssh_network.prefixlen < 24:
        sys.exit("refusing --ssh-cidr broader than /24; use a narrow admin range")
    args.ssh_cidr = str(ssh_network)

    if args.config_file and os.path.exists(args.config_file):
        os.environ.setdefault("OS_CLIENT_CONFIG_FILE", args.config_file)

    conn = openstack.connect(cloud=args.cloud)
    ensure_keypair(conn, args.pubkey)
    ensure_security_group(conn, args.ssh_cidr)
    server = ensure_server(conn)
    floating_ip = ensure_floating_ip(conn, server)
    volume = ensure_volume(conn, server)

    print("\nRESULT")
    print("server:", SERVER_NAME, server.id)
    print("floating_ip:", floating_ip.floating_ip_address)
    print("volume:", VOLUME_NAME, volume.id)
    print("ssh:", f"ssh -i ~/.ssh/p4_group09_cloud ubuntu@{floating_ip.floating_ip_address}")


if __name__ == "__main__":
    main()
