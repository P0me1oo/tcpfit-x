"""优化线路调优回归：配对隔离、真实数据校验、保留判据和恢复契约。"""
import copy
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("tcpfit_return", ROOT / "tcpfit-return.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def iperf_result(streams=1, duration=10, mbps=100, retrans=10):
    summary = {"bits_per_second": mbps * 1000000, "bytes": mbps * 1000000 * duration / 8, "seconds": duration, "retransmits": retrans}
    return {"start": {"test_start": {"protocol": "TCP", "reverse": 1, "num_streams": streams, "duration": duration, "omit": 0}},
            "end": {"streams": [{} for _ in range(streams)], "sum_sent": dict(summary), "sum_received": dict(summary)}}


class MeasurementTests(unittest.TestCase):
    def test_receiver_goodput_and_server_retrans_are_authoritative(self):
        client, server = iperf_result(mbps=97, retrans=999), iperf_result(mbps=100, retrans=17)
        value = MODULE.parse_measurement(client, server, 1, 10)
        self.assertEqual(value["receiver_mbps"], 97)
        self.assertEqual(value["sender_mbps"], 100)
        self.assertEqual(value["retransmits"], 17)
        self.assertAlmostEqual(value["estimated_retrans_pct"], 17 * 100 * 1448 / 125000000)

    def test_incomplete_and_wrong_direction_results_are_rejected(self):
        changes = [
            lambda value: value["start"]["test_start"].update(reverse=0),
            lambda value: value["start"]["test_start"].update(bidir=1),
            lambda value: value["start"]["test_start"].update(num_streams=4),
            lambda value: value["start"]["test_start"].update(omit=2),
            lambda value: value["end"]["sum_sent"].pop("retransmits"),
            lambda value: value["end"]["sum_sent"].update(retransmits=-1),
            lambda value: value["end"]["sum_sent"].update(retransmits=0.5),
            lambda value: value["end"]["sum_sent"].update(seconds=2),
            lambda value: value["end"]["sum_sent"].update(bits_per_second=float("nan")),
            lambda value: value["end"]["sum_sent"].update(bytes=1),
            lambda value: value["end"].update(streams=[]),
            lambda value: value.update(error="control connection reset"),
            lambda value: value.update(start=None),
            lambda value: value["end"].update(sum_sent=[]),
            lambda value: value["start"].update(test_start=[]),
        ]
        for change in changes:
            with self.subTest(change=change):
                server = iperf_result()
                change(server)
                with self.assertRaises(MODULE.TaskError):
                    MODULE.parse_measurement(iperf_result(), server, 1, 10)

    def test_missing_latency_is_not_zero(self):
        missing = MODULE.latency_summary("0 0\nwrong\nnan 0\n0.2 0.01\n")
        self.assertIsNone(missing["mean_ms"])
        self.assertEqual(missing["samples"], 1)
        self.assertTrue(missing["reason"])
        valid = MODULE.latency_summary("0.12 0.02\n0.14 0.02\n0.16 0.02\n")
        self.assertAlmostEqual(valid["mean_ms"], 120)

    def test_inconsistent_session_or_received_bytes_are_rejected(self):
        client, server = iperf_result(mbps=101), iperf_result(mbps=100)
        with self.assertRaises(MODULE.TaskError):
            MODULE.parse_measurement(client, server, 1, 10)
        client = iperf_result()
        client["start"]["cookie"] = "test-placeholder"
        with self.assertRaises(MODULE.TaskError):
            MODULE.parse_measurement(client, server, 1, 10)

    def test_iperf_cookie_is_not_archived(self):
        self.assertEqual(MODULE.clean_raw({"start": {"cookie": "temporary", "version": "iperf"}}), {"start": {"version": "iperf"}})


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.firewall = mock.Mock()
        args = types.SimpleNamespace(token_ttl=600)
        self.coordinator = MODULE.Coordinator(args, self.directory.name, self.directory.name, self.firewall)
        self.token = self.coordinator.token

    def tearDown(self):
        self.directory.cleanup()

    def test_bad_token_does_not_consume_pairing_or_change_firewall(self):
        self.assertEqual(self.coordinator.pair("Pair invalid", "192.0.2.1", MODULE.VERSION)[0], 403)
        self.assertIsNone(self.coordinator.peer)
        self.assertEqual(self.coordinator.token, self.token)
        self.firewall.pair.assert_not_called()
        self.assertEqual(self.coordinator.pair("Pair 无效", "192.0.2.1", MODULE.VERSION)[0], 403)

    def test_single_use_token_and_source_bound_session(self):
        status, response = self.coordinator.pair("Pair " + self.token, "192.0.2.1", MODULE.VERSION)
        self.assertEqual(status, 200)
        session = response[3:]
        self.assertIsNone(self.coordinator.token)
        self.assertTrue(self.coordinator.authenticate("Bearer " + session, "192.0.2.1"))
        self.assertFalse(self.coordinator.authenticate("Bearer " + session, "192.0.2.2"))
        self.assertEqual(self.coordinator.pair("Pair " + self.token, "192.0.2.2", MODULE.VERSION)[0], 409)
        self.assertEqual(self.coordinator.peer, "192.0.2.1")
        self.assertIsNone(self.coordinator.error)
        self.firewall.pair.assert_called_once_with("192.0.2.1")

    def test_expired_token_never_opens_test_port(self):
        self.coordinator.expires = time.monotonic() - 1
        self.assertEqual(self.coordinator.pair("Pair " + self.token, "192.0.2.1", MODULE.VERSION)[0], 410)
        self.firewall.pair.assert_not_called()

    def test_firewall_failure_aborts_pairing(self):
        self.firewall.pair.side_effect = MODULE.TaskError("permission denied")
        self.assertEqual(self.coordinator.pair("Pair " + self.token, "192.0.2.1", MODULE.VERSION)[0], 500)
        self.assertIsNotNone(self.coordinator.error)
        self.assertIsNone(self.coordinator.token)

    def test_disconnect_stops_task(self):
        self.coordinator.pair("Pair " + self.token, "192.0.2.1", MODULE.VERSION)
        self.coordinator.last_seen -= MODULE.HEARTBEAT_TIMEOUT + 1
        with self.assertRaisesRegex(MODULE.TaskError, "心跳"):
            self.coordinator.check()

    def test_paired_task_keeps_running_after_thirty_minutes_while_heartbeat_is_active(self):
        status, response = self.coordinator.pair("Pair " + self.token, "192.0.2.1", MODULE.VERSION)
        self.assertEqual(status, 200)
        later = time.monotonic() + 3600
        with mock.patch.object(MODULE.time, "monotonic", return_value=later):
            self.assertTrue(self.coordinator.authenticate("Bearer " + response[3:], "192.0.2.1"))
            self.coordinator.check()
            self.coordinator.last_seen -= MODULE.HEARTBEAT_TIMEOUT + 1
            with self.assertRaisesRegex(MODULE.TaskError, "心跳"):
                self.coordinator.check()

    def test_closed_task_cannot_pair_or_restart_a_test(self):
        self.coordinator.close()
        self.assertIsNone(self.coordinator.token)
        self.assertIsNone(self.coordinator.session)
        self.assertEqual(self.coordinator.pair("Pair " + self.token, "192.0.2.1", MODULE.VERSION)[0], 410)
        with self.assertRaises(MODULE.TaskError):
            self.coordinator.next_job()
        self.firewall.pair.assert_not_called()


class DecisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.worker = MODULE.Worker(ROOT / "tcpfit.sh")

    def rows(self, single, four, retrans_pct=0.01):
        return [{"streams": streams, "receiver_mbps": value, "estimated_retrans_pct": retrans_pct}
                for streams, values in ((1, single), (4, four)) for value in values]

    def test_no_existing_shaper_never_creates_one(self):
        rows = self.rows([600] * 3, [1000] * 3)
        self.assertIsNone(MODULE.shape_candidate(self.worker, None, rows)[0])

    def test_old_cap_cannot_be_lowered_or_raised_from_one_peak(self):
        rows = self.rows([300] * 3, [490, 510, 1000])
        self.assertIsNone(MODULE.shape_candidate(self.worker, 500, rows)[0])
        rows = self.rows([400] * 3, [510, 511, 512])
        self.assertIsNone(MODULE.shape_candidate(self.worker, 500, rows)[0])

    def test_candidate_uses_original_margin(self):
        rows = self.rows([600] * 3, [940, 960, 970])
        self.assertEqual(MODULE.shape_candidate(self.worker, 500, rows)[0], 915)

    def test_base_change_requires_single_connection_not_to_regress(self):
        before = self.rows([400, 410, 420], [700, 710, 720])
        after = self.rows([410, 420, 430], [710, 720, 730])
        self.assertTrue(MODULE.base_decision(self.worker, before, after)[0])
        after = self.rows([300, 310, 320], [800, 810, 820])
        self.assertFalse(MODULE.base_decision(self.worker, before, after)[0])

    def test_retrans_regression_rejects_faster_candidate(self):
        before = self.rows([400] * 3, [700] * 3)
        after = self.rows([600] * 3, [900] * 3, 1.5)
        self.assertFalse(MODULE.base_decision(self.worker, before, after)[0])

    def test_small_speed_noise_and_retrans_band_edge_do_not_reject_a_candidate(self):
        before = self.rows([100] * 3, [200] * 3, 0.049)
        after = self.rows([96, 97, 98], [193, 194, 195], 0.055)
        self.assertTrue(MODULE.base_decision(self.worker, before, after)[0])
        self.assertFalse(MODULE.retrans_acceptable(self.worker, before, after))

    def test_speed_tolerance_only_uses_single_connection(self):
        before = self.rows([100] * 3, [200] * 3)
        self.assertTrue(MODULE.base_decision(self.worker, before, self.rows([95] * 3, [190] * 3))[0])
        self.assertFalse(MODULE.base_decision(self.worker, before, self.rows([94.9] * 3, [220] * 3))[0])
        self.assertTrue(MODULE.base_decision(self.worker, before, self.rows([110] * 3, [100] * 3))[0])

    def test_base_acceptance_needs_no_four_connection_data(self):
        before = self.rows([100] * 3, [])
        after = self.rows([98] * 3, [])
        self.assertTrue(MODULE.base_decision(self.worker, before, after)[0])
        self.assertFalse(MODULE.base_decision(self.worker, before, self.rows([], [200] * 3))[0])

    def test_nominal_bandwidth_uses_smaller_known_value(self):
        for server, client, expected in ((1000, 200, 200), (100, 1000, 100),
                                         (1000, None, 1000), (None, 200, 200)):
            with self.subTest(server=server, client=client):
                self.assertEqual(MODULE.reference_bandwidth(server, client), expected)
        self.assertIsNone(MODULE.reference_bandwidth(None, None))
        for server, client in ((0, 1000), (-1, None), (None, 1000001), (True, 1000), (1.5, None)):
            with self.subTest(server=server, client=client), self.assertRaises(MODULE.TaskError):
                MODULE.reference_bandwidth(server, client)

    def test_obvious_outlier_is_unstable_and_cannot_be_accepted_as_improvement(self):
        before = self.rows([100] * 3, [200] * 3)
        after = self.rows([60, 100, 105], [195, 200, 205])
        after[0]["estimated_retrans_pct"] = 0.4
        self.assertFalse(MODULE.base_decision(self.worker, before, after)[0])
        self.assertTrue(any("不稳定" in reason for reason in MODULE.base_decision(self.worker, before, after)[1]))
        after[1]["estimated_retrans_pct"] = 0.4
        self.assertFalse(MODULE.base_decision(self.worker, before, after)[0])

    def test_retrans_increase_below_one_percent_is_accepted(self):
        before = self.rows([100] * 3, [200] * 3, 0.1)
        after = self.rows([120] * 3, [240] * 3, 0.3)
        self.assertTrue(MODULE.base_decision(self.worker, before, after)[0])

    def test_final_acceptance_rejects_high_retransmission_even_after_improvement(self):
        before = self.rows([100] * 3, [200] * 3, 6)
        after = self.rows([100] * 3, [200] * 3, 1.2)
        kept, reasons = MODULE.base_decision(self.worker, before, after)
        self.assertFalse(kept)
        self.assertIn("单连接估算重传比超过 1%", reasons)

    def test_raised_shaper_requires_original_throughput_band_and_stable_excess(self):
        reference = self.rows([600] * 3, [940] * 3)
        self.assertTrue(MODULE.shape_decision(self.worker, reference, self.rows([550] * 3, [800] * 3), 500, 900, 75)[0])
        self.assertFalse(MODULE.shape_decision(self.worker, reference, self.rows([550] * 3, [600] * 3), 500, 900, 75)[0])
        self.assertFalse(MODULE.shape_decision(self.worker, reference, self.rows([550] * 3, [499, 800, 900]), 500, 900, 75)[0])
        self.assertFalse(MODULE.shape_decision(self.worker, reference, self.rows([550] * 3, [800] * 3, 1.5), 500, 900, 75)[0])


class QueueTests(unittest.TestCase):
    MODERN_FQ = (
        "qdisc fq 8001: root refcnt 2 limit 10000p flow_limit 100p buckets 1024 orphan_mask 1023 "
        "bands 3 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1 weights 589824 196608 65536 "
        "quantum 3028b initial_quantum 15140b low_rate_threshold 550Kbit refill_delay 40ms "
        "timer_slack 10us horizon 10s horizon_drop\n"
    )

    def capture(self, qdisc, classes="", filters=""):
        def run(args, **kwargs):
            if args[-1] == "help":
                return subprocess.CompletedProcess(args, 1, "", "Usage: ... fq\n")
            output = qdisc if "qdisc" in args else classes if "class" in args else filters
            return subprocess.CompletedProcess(args, 0, output, "")
        with mock.patch.object(MODULE, "command", side_effect=run):
            return MODULE.QueueState.capture("eth0")

    def test_modern_fq_parameters_are_preserved(self):
        state = self.capture(self.MODERN_FQ)
        expected = (
            "limit 10000 flow_limit 100 buckets 1024 orphan_mask 1023 "
            "bands 3 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1 weights 589824 196608 65536 "
            "quantum 3028 initial_quantum 15140 low_rate_threshold 550Kbit refill_delay 40ms "
            "timer_slack 10us horizon 10s horizon_drop"
        ).split()
        self.assertEqual(state["qdiscs"][0]["options"], expected)

    def test_modern_fq_replay_supports_both_weights_parsers(self):
        for leaf in (False, True):
            for skips_first in (False, True):
                with self.subTest(leaf=leaf, skips_first=skips_first):
                    raw = "qdisc mq 1: root\n" + self.MODERN_FQ.replace("root", "parent 1:1") if leaf else self.MODERN_FQ
                    state = self.capture(raw, "class mq 1:1 root\n" if leaf else "")
                    original = copy.deepcopy(state)
                    expected = list(state["qdiscs"][-1]["options"])
                    if skips_first:
                        index = expected.index("weights") + 1
                        expected.insert(index, expected[index])
                    calls = []
                    def run(args, **kwargs):
                        calls.append(args)
                        if args[-1] == "help":
                            self.assertEqual(args[:4], ["tc", "qdisc", "add", "fq"])
                            self.assertNotIn("dev", args)
                            error = "Usage: ... fq\n" if args[4:-1] == expected else 'Illegal "weights" element\n'
                            return subprocess.CompletedProcess(args, 1, "", error)
                        return subprocess.CompletedProcess(args, 0, "", "")
                    with mock.patch.object(MODULE, "command", side_effect=run):
                        MODULE.QueueState.restore(state)
                    replay = next(args for args in calls if "replace" in args and "fq" in args)
                    self.assertEqual(replay[replay.index("fq") + 1:], expected)
                    first_delete = next(index for index, args in enumerate(calls) if "del" in args)
                    self.assertGreater(first_delete, 0)
                    self.assertTrue(all(args[-1] == "help" for args in calls[:first_delete]))
                    self.assertEqual(state, original)

    def test_modern_fq_parser_rejection_stops_before_mutation(self):
        state = self.capture(self.MODERN_FQ)
        for operation in (lambda: MODULE.QueueState.capture("eth0"), lambda: MODULE.QueueState.restore(state)):
            calls = []
            def run(args, **kwargs):
                calls.append(args)
                if args[-1] == "help":
                    return subprocess.CompletedProcess(args, 1, "", 'What is "weights"?\nUsage: ... fq\n')
                return subprocess.CompletedProcess(args, 0, self.MODERN_FQ if "qdisc" in args else "", "")
            with mock.patch.object(MODULE, "command", side_effect=run), self.assertRaises(MODULE.TaskError):
                operation()
            self.assertTrue(all("show" in args or args[-1] == "help" for args in calls))

    def test_custom_fq_parameters_are_preserved(self):
        state = self.capture("qdisc fq 8001: root refcnt 2 limit 40960p flow_limit 8192p buckets 1024 orphan_mask 1023 quantum 1514b initial_quantum 15140b maxrate 1Gbit\n")
        self.assertIn("40960", state["qdiscs"][0]["options"])
        self.assertIn("1Gbit", state["qdiscs"][0]["options"])

    def test_mq_keeps_each_leaf_separately(self):
        state = self.capture("qdisc mq 0: root\nqdisc fq 8001: parent :1 limit 10000p\nqdisc fq_codel 8002: parent :2 limit 10240p ecn\n", "class mq :1 root\nclass mq :2 root\n")
        self.assertEqual([row["kind"] for row in state["qdiscs"]], ["mq", "fq", "fq_codel"])

    def test_automatic_mq_restore_does_not_reuse_a_leaf_handle(self):
        state = self.capture("qdisc mq 0: root\nqdisc fq 1: parent :1 limit 10000p\n", "class mq :1 root\n")
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        with mock.patch.object(MODULE, "command", side_effect=run):
            MODULE.QueueState.restore(state)
        root = next(args for args in calls if "replace" in args and "root" in args)
        self.assertEqual(root[root.index("handle") + 1], "2:")

    def test_automatic_zero_handle_queue_already_matches_snapshot(self):
        for kind, options in (("fq", "limit 10000p flow_limit 100p"), ("fq_codel", "limit 10240p ecn")):
            with self.subTest(kind=kind):
                original = "qdisc {} 0: root refcnt 2 {}\n".format(kind, options)
                state = self.capture(original)
                def run(args, **kwargs):
                    if "change" in args or "replace" in args:
                        raise MODULE.TaskError("内核自动队列已经恢复，不应再次修改")
                    return subprocess.CompletedProcess(args, 0, original if "show" in args else "", "")
                with mock.patch.object(MODULE, "command", side_effect=run):
                    MODULE.QueueState.restore(state)

    def test_automatic_zero_handle_with_different_options_is_recreated(self):
        state = self.capture("qdisc fq 0: root limit 20000p flow_limit 200p\n")
        actual = "qdisc fq 0: root limit 10000p flow_limit 100p\n"
        def run(args, **kwargs):
            nonlocal actual
            if "change" in args:
                raise MODULE.TaskError("Qdisc not found. To create specify NLM_F_CREATE flag.")
            if "replace" in args:
                actual = "qdisc fq 8001: root " + " ".join(args[args.index("fq") + 1:]) + "\n"
            return subprocess.CompletedProcess(args, 0, actual if "show" in args else "", "")
        with mock.patch.object(MODULE, "command", side_effect=run):
            MODULE.QueueState.restore(state)
        restored = self.capture(actual)
        self.assertEqual(restored["qdiscs"][0]["options"], state["qdiscs"][0]["options"])
        self.assertEqual(restored["qdiscs"][0]["kind"], "fq")

    def test_unrestorable_queue_and_filters_fail_before_mutation(self):
        for qdisc, filters in (("qdisc cake 1: root bandwidth 1Gbit\n", ""), ("qdisc fq 1: root future_option value\n", ""), ("qdisc fq 1: root\n", "filter protocol ip pref 1 flower")):
            with self.subTest(qdisc=qdisc), self.assertRaises(MODULE.TaskError):
                self.capture(qdisc, filters=filters)

    def test_htb_rate_and_ceil_must_agree(self):
        qdisc = "qdisc htb 1: root refcnt 2 r2q 10 default 0x10 direct_packets_stat 0 direct_qlen 1000\nqdisc fq 10: parent 1:10 limit 40960p flow_limit 8192p maxrate 1Gbit\n"
        classes = "class htb 1:10 root leaf 10: prio 0 quantum 1514 rate 1Gbit ceil 1Gbit linklayer ethernet burst 500000b/1 mpu 0b cburst 500000b/1 mpu 0b level 0\n"
        state = self.capture(qdisc, classes)
        self.assertEqual(state["rate"], 1000)
        options = state["classes"][0]["options"]
        self.assertEqual(options[options.index("quantum") + 1], "1514")
        self.assertEqual(options[options.index("mpu") + 1], "0")
        with self.assertRaises(MODULE.TaskError):
            self.capture(qdisc, classes.replace("ceil 1Gbit", "ceil 2Gbit"))

    def test_htb_compact_mpu_output_keeps_burst_and_mpu(self):
        qdisc = "qdisc htb 1: root r2q 10 default 0x10\nqdisc fq 10: parent 1:10 limit 10000p\n"
        classes = "class htb 1:10 root leaf 10: prio 0 quantum 1514 rate 321Mbit ceil 321Mbit linklayer ethernet burst 160500b/1mpu 64b cburst 160500b/1mpu 64b level 0\n"
        state = self.capture(qdisc, classes)
        spaced = self.capture(qdisc, classes.replace("/1mpu", "/1 mpu"))
        self.assertEqual(state["classes"], spaced["classes"])
        options = state["classes"][0]["options"]
        self.assertEqual(options[options.index("burst") + 1], "160500b/1")
        self.assertEqual(options[options.index("cburst") + 1], "160500b/1")
        self.assertEqual(options[options.index("mpu") + 1], "64")
        with self.assertRaises(MODULE.TaskError):
            self.capture(qdisc, classes.replace("cburst 160500b/1mpu 64b", "cburst 160500b/1mpu 32b"))


class RouteRestoreTests(unittest.TestCase):
    def test_ra_lifetime_countdown_is_not_a_configuration_change(self):
        before = "default via fe80::1 dev eth0 proto ra metric 1024 expires 1787sec hoplimit 64 pref medium"
        current = before.replace("1787sec", "1460sec")
        self.assertEqual(MODULE.Snapshot.route_config(before), MODULE.Snapshot.route_config(current))
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            output = current + "\n" if args[:4] == ["ip", "-6", "route", "show"] else ""
            return subprocess.CompletedProcess(args, 0, output, "")
        with mock.patch.object(MODULE, "command", side_effect=run), mock.patch.object(MODULE.QueueState, "restore"):
            MODULE.Snapshot.restore({"sysctl": {}, "files": {}, "routes": {"-6": [before]}, "queue": {}, "service_active": False})
        self.assertFalse(any("replace" in args or "del" in args for args in calls))

    def test_expiry_display_suffix_is_converted_and_elapsed_time_is_subtracted(self):
        with mock.patch.object(MODULE.time, "time", return_value=110):
            args = MODULE.Snapshot.route_restore_args("default via fe80::1 dev eth0 expires 600sec", 100)
        self.assertEqual(args[-2:], ["expires", "590"])


if __name__ == "__main__":
    unittest.main()
