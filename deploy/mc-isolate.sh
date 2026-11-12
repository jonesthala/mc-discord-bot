#!/bin/sh
# Keeps the Minecraft containers (Docker network "mcnet") away from the home network.
# They may still reach the internet (downloads, Mojang login), and replies to players who
# connect to them (through the tunnel or from the LAN) are still allowed.
#
#   mc-isolate.sh apply    add the rules (also what the systemd unit runs)
#   mc-isolate.sh remove   take them out again
set -eu

SUBNET="172.30.30.0/24"   # must match NETWORK_SUBNET in bot/servers.py
CHAIN="MCBOT-ISOLATE"

remove() {
  while iptables -D DOCKER-USER -s "$SUBNET" -j "$CHAIN" 2>/dev/null; do :; done
  iptables -F "$CHAIN" 2>/dev/null || true
  iptables -X "$CHAIN" 2>/dev/null || true
}

if [ "${1:-apply}" = "remove" ]; then
  remove
  exit 0
fi

remove
iptables -N DOCKER-USER 2>/dev/null || true
iptables -N "$CHAIN"
iptables -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16; do
  iptables -A "$CHAIN" -d "$net" -j DROP
done
iptables -I DOCKER-USER 1 -s "$SUBNET" -j "$CHAIN"
