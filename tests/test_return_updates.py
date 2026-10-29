"""回国路径、窗口恢复、流量预算及持久化回归，不改动本机网络。"""
import copy
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from test_return import MODULE, ROOT
from test_return_records import snapshot


class RouteRecoveryTests(unittest.TestCase):
    def recover(self, before, current, *, iface="eth1", family="-4"):
        state = {"sysctl": {}, "files": {}, "routes": {family: before},
                 "queue": {"iface": iface}, "service_active": False}
        routes, writes = list(current), []

        def run(args, **kwargs):
            if args[:4] == ["ip", family, "route", "show"]:
                return subprocess.CompletedProcess(args, 0, "\n".join(routes), "")
            if args[:4] == ["ip", family, "route", "replace"]:
                writes.append(args)
                replacement = " ".join(args[4:])
                dev = replacement.split()[replacement.split().index("dev") + 1]
                routes[:] = [line for line in routes if " dev " + dev + " " not in " " + line + " "] + [replacement]
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(MODULE, "command", side_effect=run), mock.patch.object(MODULE.QueueState, "restore"):
            MODULE.Snapshot.restore(state)
        return routes, writes

    def test_changed_gateway_and_metric_are_preserved_and_other_interface_is_untouched(self):
        old = "default via 192.0.2.1 dev eth1 proto dhcp metric 42 initcwnd 20 initrwnd 24"
        current = "default via 192.0.2.254 dev eth1 proto dhcp src 192.0.2.10 metric 10 onlink initcwnd 32 initrwnd 32"
        unrelated = "default via 198.51.100.1 dev eth0 metric 1 initcwnd 18"
        routes, writes = self.recover([old], [unrelated, current])
        self.assertIn(unrelated, routes)
        self.assertEqual(len(writes), 1)
        self.assertIn("via 192.0.2.254 dev eth1 proto dhcp src 192.0.2.10 metric 10 onlink initcwnd 20 initrwnd 24", " ".join(writes[0]))
        self.assertNotIn("192.0.2.1", writes[0])

    def test_policy_route_restores_only_windows_and_preserves_ipv6_expiry(self):
        old = "default via fe80::1 dev eth1 table 100 metric 42 expires 600sec"
        current = "default via fe80::2 dev eth1 table 100 metric 20 expires 480sec initcwnd 32 initrwnd 32"
        routes, writes = self.recover([old], [current], family="-6")
        self.assertIn("table 100 metric 20 expires 480", " ".join(writes[0]))
        self.assertNotIn("initcwnd", routes[0])

    def test_missing_and_ambiguous_routes_are_not_recreated(self):
        old = "default via 192.0.2.1 dev eth1 metric 42"
        for current in ([], ["default dev eth1 metric 10", "default dev eth1 metric 20"]):
            with self.subTest(current=current), self.assertRaisesRegex(MODULE.TaskError, "无法唯一对应"):
                self.recover([old], current)

    def test_new_defaults_are_not_deleted(self):
        old = "default dev eth1 metric 10"
        new = "default via 192.0.2.254 dev eth1 metric 20"
        routes, writes = self.recover([old], [old, new])
        self.assertEqual(routes, [old, new])
        self.assertFalse(writes)

    def test_actual_family_and_policy_table_select_the_paired_path(self):
        with mock.patch.object(MODULE, "command", return_value=subprocess.CompletedProcess([], 0, "2001:db8::1 via fe80::1 dev eth1 table 100 src 2001:db8::2", "")) as run:
            self.assertEqual(MODULE.return_route("2001:db8::1", 6), ("eth1", "100"))
        run.assert_called_once_with(["ip", "-6", "route", "get", "2001:db8::1"])

    def test_zero_selection_accepts_current_gateway_but_requires_original_windows(self):
        before = snapshot()
        before["queue"]["iface"] = "eth0"
        after = copy.deepcopy(before)
        after["routes"]["-4"] = ["default via 192.0.2.254 dev eth0 metric 99"]
        self.assertTrue(MODULE.Snapshot.matches_restored(after, before))
        after["routes"]["-4"][0] += " initcwnd 32"
        with self.assertRaises(MODULE.TaskError):
            MODULE.Snapshot.matches_restored(after, before)

    def test_route_scope_does_not_restore_other_tables_or_protocol_families(self):
        state = {"queue": {"iface": "eth1"}, "route_scope": {"family": "-4", "table": "100"},
                 "routes": {"-4": ["default dev eth1 table 100 initcwnd 20", "default dev eth1 table 200 initcwnd 24"],
                            "-6": ["default dev eth1 initcwnd 28"]}}
        current = {"-4": ["default dev eth1 table 100 initcwnd 20", "default dev eth1 table 200 initcwnd 99"],
                   "-6": ["default dev eth1 initcwnd 99"]}
        MODULE.Snapshot.verify_route_windows(state, current)
        self.assertEqual(len(list(MODULE.Snapshot.route_pairs(state, current))), 1)


class TrafficTests(unittest.TestCase):
    def book(self, directory, limit=1):
        coordinator = types.SimpleNamespace(results=[], run_dir=Path(directory), check=mock.Mock())
        worker = mock.Mock()
        def sample(action, streams, duration, **kwargs):
            coordinator.results.append({"streams": streams, "receiver_mbps": 960, "sender_mbps": 1000,
                                        "estimated_retrans_pct": 0, "retransmits": 0,
                                        "sender_bytes": 1250000000, "receiver_bytes": 1200000000})
        worker.run.side_effect = sample
        return MODULE.MeasurementBook(worker, coordinator, directory, snapshot, 2, 10, limit), worker

    def test_budget_stops_before_next_sample_and_keeps_completed_measurement(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            book, worker = self.book(directory)
            with self.assertRaises(MODULE.TrafficBudgetReached):
                book.measure_group("初值测速")
            self.assertEqual(worker.run.call_count, 1)
            self.assertEqual([row["status"] for row in book.records], ["valid", "skipped"])
            self.assertIsNotNone(MODULE.fastest_configuration(book, 1))
            traffic = MODULE.read_json(Path(directory) / "traffic.json")
            self.assertEqual(traffic["sender_bytes"], 1250000000)
            self.assertEqual(traffic["receiver_bytes"], 1200000000)
            self.assertTrue(traffic["limit_reached"])
            self.assertFalse(traffic["incomplete"])

    def test_default_has_no_budget_and_counts_all_repeated_transfers(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            book, worker = self.book(directory, None)
            book.measure_group("初值测速")
            book.measure_group("候选测速")
            self.assertEqual(worker.run.call_count, 4)
            self.assertEqual(book.traffic_summary()["sender_bytes"], 5000000000)
            self.assertFalse(book.traffic_summary()["limit_reached"])

    def test_failed_transfer_is_marked_incomplete_not_counted_as_zero_success(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            book, worker = self.book(directory)
            worker.run.side_effect = MODULE.TaskError("连接断开")
            with self.assertRaises(MODULE.TaskError):
                book.measure_group("候选测速")
            self.assertTrue(book.traffic_summary()["incomplete"])


class PersistenceTests(unittest.TestCase):
    def test_saving_existing_windows_refreshes_persistence_without_retesting(self):
        state = snapshot()
        state["route_scope"] = {"family": "-4", "table": "main"}
        state["queue"]["iface"] = "eth0"
        state["routes"]["-4"][0] += " initcwnd 20 initrwnd 24"
        worker = mock.Mock()
        self.assertTrue(MODULE.persist_selected_windows(worker, state))
        worker.run.assert_called_once_with("persist-windows", "initcwnd", "20", "initrwnd", "24")
        worker.run.side_effect = MODULE.TaskError("持久化失败")
        with self.assertRaises(MODULE.TaskError):
            MODULE.persist_selected_windows(worker, state)

    def test_restore_removes_new_dropin_and_restores_existing_custom_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old.network.d" / MODULE.WINDOW_DROPIN
            new = root / "new.network.d" / MODULE.WINDOW_DROPIN
            old.parent.mkdir(); new.parent.mkdir()
            old.write_text("old", encoding="utf-8")
            new.write_text("new", encoding="utf-8")
            state = {"sysctl": {}, "routes": {}, "queue": {}, "service_active": False,
                     "files": {MODULE.CONFIG_FILES[0]: None, str(old): {"data": "b2xk", "mode": 420, "uid": 0, "gid": 0}}}
            with mock.patch.object(MODULE, "NETWORKD_DIR", root), \
                    mock.patch.object(MODULE, "command", return_value=subprocess.CompletedProcess([], 0, "inactive", "")), \
                    mock.patch.object(MODULE.Snapshot, "restore_files") as restore, \
                    mock.patch.object(MODULE.QueueState, "restore"):
                MODULE.Snapshot.restore(state)
            restored = restore.call_args.args[0]
            self.assertIsNone(restored[str(new)])
            self.assertEqual(restored[str(old)]["data"], "b2xk")

    def shell(self, script):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).as_posix()
            setup = r'''
source "$1"
task_dir="$2"
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
systemctl(){ printf '%s\n' "$*" >> "$task_dir/systemctl.log"; [ "$1" != --version ] || echo 'systemd 255'; return 0; }
networkctl(){ [ "${2:-}" = eth1 ] || return 1; printf 'State: routable (configured)\nNetwork File: /run/systemd/network/10-eth1.network\n'; }
iface_is_ppp(){ return 1; }
ip(){ printf 'default via 192.0.2.254 dev eth1 proto dhcp metric 10 onlink\n'; }
'''
            result = subprocess.run([shutil.which("bash") or "bash", "-c", setup + script, "test-persistence",
                                     (ROOT / "tcpfit.sh").as_posix(), path], capture_output=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result.stdout

    def test_networkd_configuration_preserves_selected_values_and_does_not_reload_network(self):
        self.shell(r'''
persist_initcwnd eth1 initcwnd 20 initrwnd 24 || exit 1
grep -q 'InitialCongestionWindow=20' "$NETWORKD_DIR/10-eth1.network.d/50-tcpfit-initcwnd.conf" || exit 2
grep -q 'InitialAdvertisedReceiveWindow=24' "$NETWORKD_DIR/10-eth1.network.d/50-tcpfit-initcwnd.conf" || exit 3
if grep -E '(restart|reload) systemd-networkd' "$task_dir/systemctl.log"; then exit 4; fi
remove_initcwnd_persistence || exit 5
test ! -e "$NETWORKD_DIR/10-eth1.network.d/50-tcpfit-initcwnd.conf"
''')

    def test_ppp_hook_is_limited_to_paired_interface_and_retains_custom_windows(self):
        self.shell(r'''
iface_is_ppp(){ return 0; }
persist_initcwnd ppp0 initcwnd 20 initrwnd 24 || exit 1
sh -n "$PPP_HOOK" || exit 2
sh "$PPP_HOOK" ppp1 || exit 3
grep -q 'initcwnd 20 initrwnd 24' "$PPP_HOOK" || exit 4
test ! -e "$INITCWND_UNIT"
''')

    def test_non_networkd_interface_uses_enabled_boot_service(self):
        self.shell(r'''
networkd_manages(){ return 1; }
IP_FAMILY=-6
TCPFIT_ROUTE_TABLE=100
persist_initcwnd eth1 initcwnd 20 initrwnd 24 || exit 1
sh -n "$INITCWND_SCRIPT" || exit 2
grep -q 'enable --now tcpfit-initcwnd.service' "$task_dir/systemctl.log" || exit 3
grep -q 'IP_FAMILY=-6' "$INITCWND_SCRIPT" || exit 4
grep -q 'TCPFIT_ROUTE_TABLE=100' "$INITCWND_SCRIPT"
''')


if __name__ == "__main__":
    unittest.main()
