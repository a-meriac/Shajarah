#!/usr/bin/env bash
# Emulated topology (Linux only, needs root):
#
#   [ep-cli] --wifi0 10.1.0.2 ---- 10.1.0.1 --\
#            --cell0 10.2.0.2 ---- 10.2.0.1 ---[ep-rtr]-- 10.9.0.1 ---- 10.9.0.2 [ep-srv]
#            --sat0  10.3.0.2 ---- 10.3.0.1 --/    \---- 10.8.0.1 ---- 10.8.0.2 [ep-org]
#
# ep-cli: browser/client proxy. ep-srv: server proxy. ep-org: origin server ("the internet").
# netem shaping goes on both ends of each access link (see shaper.py). Policy routing makes a
# socket bound to 10.X.0.2 leave through that interface, which is how the tunnel picks a path.
#
# Usage: sudo emulation/netns_setup.sh up|down
set -euo pipefail

IFACES=(wifi0:1 cell0:2 sat0:3)
NS=(ep-cli ep-rtr ep-srv ep-org)

down() {
  for ns in "${NS[@]}"; do ip netns del "$ns" 2>/dev/null || true; done
}

up() {
  down
  for ns in "${NS[@]}"; do
    ip netns add "$ns"
    ip -n "$ns" link set lo up
  done
  ip netns exec ep-rtr sysctl -qw net.ipv4.ip_forward=1

  local table=100
  for pair in "${IFACES[@]}"; do
    local name=${pair%%:*} n=${pair##*:}
    ip link add "$name" netns ep-cli type veth peer name "r-$name" netns ep-rtr
    ip -n ep-cli addr add "10.$n.0.2/24" dev "$name"
    ip -n ep-rtr addr add "10.$n.0.1/24" dev "r-$name"
    ip -n ep-cli link set "$name" up
    ip -n ep-rtr link set "r-$name" up
    table=$((100 + n))
    ip -n ep-cli route add default via "10.$n.0.1" dev "$name" table "$table"
    ip -n ep-cli rule add from "10.$n.0.2" table "$table" priority "$table"
  done
  # Unbound sockets default to Wi-Fi.
  ip -n ep-cli route add default via 10.1.0.1 dev wifi0

  ip link add srv0 netns ep-srv type veth peer name r-srv0 netns ep-rtr
  ip -n ep-srv addr add 10.9.0.2/24 dev srv0
  ip -n ep-rtr addr add 10.9.0.1/24 dev r-srv0
  ip -n ep-srv link set srv0 up
  ip -n ep-rtr link set r-srv0 up
  ip -n ep-srv route add default via 10.9.0.1

  ip link add org0 netns ep-org type veth peer name r-org0 netns ep-rtr
  ip -n ep-org addr add 10.8.0.2/24 dev org0
  ip -n ep-rtr addr add 10.8.0.1/24 dev r-org0
  ip -n ep-org link set org0 up
  ip -n ep-rtr link set r-org0 up
  ip -n ep-org route add default via 10.8.0.1

  # Only the server proxy may reach the origin directly; the client must go through the tunnel
  # (the plain-TCP baseline also goes through the server proxy, so every config pays the hop).
  ip netns exec ep-rtr iptables -A FORWARD -s 10.0.0.0/14 -d 10.8.0.0/24 -j DROP

  # Internet path server-proxy <-> origin: fixed 15 ms one-way, applied on the router side.
  ip netns exec ep-rtr tc qdisc add dev r-org0 root netem delay 15ms
  ip netns exec ep-org tc qdisc add dev org0 root netem delay 15ms

  echo "netns up: ${NS[*]}"
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  *) echo "usage: $0 up|down" >&2; exit 2 ;;
esac
