#!/bin/bash
set -euo pipefail
test "$(readlink /proc/self/ns/net)" != "$(readlink /proc/1/ns/net)"
test_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$test_root/tcpfit.sh"
task_dir=$(mktemp -d "${TMPDIR:-/tmp}/tcpfit-routes.XXXXXX")
trap 'rm -rf -- "$task_dir"' EXIT
ip link add tf-test0 type dummy
ip link add tf-test1 type dummy
ip link set tf-test0 up
ip link set tf-test1 up
ip addr add 192.0.2.2/24 dev tf-test1
ip -4 route add default via 192.0.2.1 dev tf-test1 proto dhcp metric 10 onlink
ip -4 route add default dev tf-test0 metric 1 initcwnd 18
TCPFIT_IF=tf-test1
IP_FAMILY=-4
route_restore_windows tf-test1 initcwnd 20 initrwnd 24
ip -4 route show default dev tf-test1 | grep -q 'via 192.0.2.1.*proto dhcp.*metric 10.*onlink.*initcwnd 20.*initrwnd 24'
ip -4 route show default dev tf-test0 | grep -q 'initcwnd 18'
echo 'PASS IPv4 targeted route and attributes'

STATE_DIR="$task_dir/state"
ROUTE_HOOK="$task_dir/dispatcher/50-tcpfit-initcwnd"
INITCWND_UNIT="$task_dir/system/tcpfit-initcwnd.service"
INITCWND_SCRIPT="$task_dir/bin/tcpfit-initcwnd.sh"
INITCWND_MARKER="$STATE_DIR/initcwnd.owned"
INITCWND_VALS="$STATE_DIR/initcwnd.vals"
PPP_HOOK="$task_dir/ppp/50-tcpfit"
QDISC_SCRIPT="$task_dir/bin/qdisc.sh"
NETWORKD_DIR="$task_dir/network"
mkdir -p "$STATE_DIR" "$task_dir/system" "$task_dir/bin" "$task_dir/ppp" "$task_dir/dispatcher" "$NETWORKD_DIR"
SNAPSHOT="$STATE_DIR/pre-tune.snapshot"
printf '# route: default via 192.0.2.1 dev tf-test1 proto dhcp metric 42\n' > "$SNAPSHOT"
remember_route_windows tf-test1
systemctl(){ return 0; }
networkd_manages(){ return 1; }
persist_initcwnd tf-test1 initcwnd 20 initrwnd 24
ip -4 route replace default via 192.0.2.254 dev tf-test1 metric 10 onlink
sh "$INITCWND_SCRIPT"
ip -4 route show default dev tf-test1 | grep -q 'via 192.0.2.254.*metric 10.*onlink.*initcwnd 20.*initrwnd 24'
echo 'PASS generated boot script uses current gateway'

IP_FAMILY=-6
TCPFIT_ROUTE_TABLE=100
ip -6 addr add 2001:db8:1::2/64 dev tf-test1 nodad
ip -6 route add table 100 default via 2001:db8:1::1 dev tf-test1 metric 100 expires 600
remember_route_windows tf-test1
route_restore_windows tf-test1 initcwnd 20 initrwnd 24
ip -6 route show table 100 default | grep -q 'initcwnd 20.*initrwnd 24'
ip -6 route show table 100 default | grep -q expires
persist_initcwnd tf-test1 initcwnd 24 initrwnd 28
sh "$INITCWND_SCRIPT"
ip -6 route show table 100 default | grep -q 'initcwnd 24.*initrwnd 28'
ip -6 route show table 100 default | grep -q expires
echo 'PASS IPv6 policy route, expiry and boot script'

IP_FAMILY=-4
TCPFIT_ROUTE_TABLE=main
iface_is_ppp(){ return 0; }
touch "$INITCWND_MARKER"
write_ppp_hook tf-test1 initcwnd 22 initrwnd 26
sh "$PPP_HOOK" tf-test0
ip -4 route show default dev tf-test0 | grep -q 'initcwnd 18'
sh "$PPP_HOOK" tf-test1
ip -4 route show default dev tf-test1 | grep -q 'initcwnd 22.*initrwnd 26'
echo 'PASS PPP hook only applies to its interface'

write_dispatcher_hook tf-test1 initcwnd 26 initrwnd 30
sh "$ROUTE_HOOK"
ip -4 route show default dev tf-test1 | grep -q 'initcwnd 26.*initrwnd 30'
route_restore_windows tf-test1
! ip -4 route show default dev tf-test1 | grep -Eq 'initcwnd|initrwnd'
echo 'PASS dispatcher hook and window removal'
route_restore_windows tf-test1 initcwnd 32 initrwnd 32
restore_window_baselines
! ip -4 route show default dev tf-test1 | grep -Eq 'initcwnd|initrwnd'
! ip -6 route show table 100 default | grep -Eq 'initcwnd|initrwnd'
ip -4 route show default dev tf-test1 | grep -q 'via 192.0.2.254'
ip -4 route show default dev tf-test0 | grep -q 'initcwnd 18'
test ! -e "$STATE_DIR/route-windows.before"
echo 'PASS rollback of both families keeps current routes and legacy baseline'

route_restore_windows tf-test1 initcwnd 20 initrwnd 24
printf '%s\n' '-4 main tf-test0' > "$INITCWND_MARKER"
clear_owned_initcwnd
ip -4 route show default dev tf-test1 | grep -q 'initcwnd 20.*initrwnd 24'
test -f "$INITCWND_MARKER"
echo 'PASS ownership from another interface does not clear custom windows'

iface_is_ppp(){ return 1; }
QDISC_UNIT="$task_dir/system/tcpfit-qdisc.service"
other_queue=$(tc qdisc show dev tf-test0)
write_qdisc 100 tf-test1
bash "$QDISC_SCRIPT"
tc class show dev tf-test1 | grep -q 'rate 100Mbit'
test "$(tc qdisc show dev tf-test0)" = "$other_queue"
test "$(cat "$STATE_DIR/qdisc.iface")" = tf-test1
echo 'PASS persistent shaping uses the paired interface'
