"""优化线路调优逐次记录、配置选择与无效数据回归；只使用临时目录和模拟测量。"""
import base64
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

from test_return_buffers import MODULE, ROOT, buffer_state, rows


def snapshot(maximum=16, cc="bbr"):
    return {
        "sysctl": dict({key: " ".join(map(str, value)) for key, value in buffer_state(maximum).items()},
                       **{"net.ipv4.tcp_congestion_control": cc}),
        "files": {}, "routes": {"-4": ["default via 192.0.2.1 dev eth0"]},
        "queue": {"rate": None, "limited_fq": False, "qdiscs": [{"kind": "fq", "options": []}]},
        "service_active": False, "captured_at": time.time(),
    }


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.current = snapshot()
        self.worker = mock.Mock()
        self.coordinator = types.SimpleNamespace(results=[], run_dir=self.directory, check=mock.Mock(),
                                                 started=time.monotonic())
        self.book = MODULE.MeasurementBook(self.worker, self.coordinator, self.directory,
                                          lambda: copy.deepcopy(self.current), 2, 10)

        def sample(action, streams, duration, **kwargs):
            self.assertEqual(action, "sample")
            self.assertEqual(duration, 10)
            self.coordinator.results.append(next(row for row in rows() if row["streams"] == streams))

        self.worker.run.side_effect = sample

    def tearDown(self):
        self.temp.cleanup()

    def test_default_and_custom_counts_are_totals_for_each_requested_mode(self):
        with redirect_stdout(io.StringIO()):
            baseline = self.book.measure_group("基线")
            trial = self.book.measure_group("试调", modes=(4,))
            custom = self.book.measure_group("自定义", modes=(1,), repeats=5)
            single = self.book.measure_group("单次评估", modes=(1, 4), repeats=1)
        self.assertEqual([row["streams"] for row in baseline], [1, 1])
        self.assertEqual([row["streams"] for row in trial], [4, 4])
        self.assertEqual(len(custom), 5)
        self.assertEqual([row["streams"] for row in single], [1, 4])
        for row in single:
            self.assertEqual(row["repeats"], 1)
            self.assertEqual(row["status"], "valid")
            self.assertEqual(MODULE.measurement_issues(single, row["streams"]), [])
            self.assertEqual(MODULE.median_metric(single, row["streams"], "receiver_mbps"), row["receiver_mbps"])
        self.assertTrue(MODULE.base_decision(self.worker, single, single)[0])
        self.assertEqual(self.worker.run.call_count, 11)
        self.assertEqual([row["number"] for row in self.book.records], list(range(1, 12)))
        self.assertEqual(len(self.book.configurations), 1)
        self.assertEqual(json.loads((self.directory / "measurement-index.json").read_text(encoding="utf-8")), self.book.records)

    def test_full_configuration_is_saved_before_measurement_and_ids_track_all_parameters(self):
        first = self.book.register()
        self.current["sysctl"]["net.ipv4.tcp_congestion_control"] = "cubic"
        second = self.book.register()
        self.assertNotEqual(first, second)
        self.assertEqual(self.book.configurations[first]["sysctl"]["net.ipv4.tcp_congestion_control"], "bbr")
        saved = json.loads((self.directory / "configurations" / (second + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(saved, self.current)
        self.current["captured_at"] += 10
        self.current["queue"]["raw_qdisc"] = "统计信息变化"
        self.assertEqual(self.book.register(), second)

    def test_mid_group_failure_keeps_valid_failed_and_skipped_rows(self):
        original = self.worker.run.side_effect
        calls = []

        def fail_second(*args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                raise MODULE.TaskError("重传数据缺失")
            return original(*args, **kwargs)

        self.worker.run.side_effect = fail_second
        with self.assertRaises(MODULE.TaskError):
            self.book.measure_group("最终验证", repeats=4)
        self.assertEqual([row["status"] for row in self.book.records], ["valid", "failed", "skipped", "skipped"])
        self.assertIn("未完成验证", self.book.records[0]["decision"])
        self.assertNotIn("estimated_retrans_pct", self.book.records[1])
        self.assertTrue(MODULE.measurement_issues(self.book.records, 1))
        output = io.StringIO()
        with redirect_stdout(output):
            MODULE.measurement_table(self.book, "C1")
        lines = [line for line in output.getvalue().splitlines() if " | " in line]
        self.assertEqual(len(lines), 2)
        self.assertIn("失败", lines[1])
        self.assertIn("跳过", lines[1])
        self.assertIn("未完成", lines[1])
        self.assertIn("100.00 / 0.010", lines[1])

    def test_selection_accepts_rejected_and_unstable_valid_samples_and_rejects_invalid_numbers(self):
        with redirect_stdout(io.StringIO()):
            self.book.measure_group("基线")
            self.current = snapshot(14, cc="cubic")
            self.book.measure_group("回退候选", modes=(4,))
        self.book.records[-1].update(decision="已回退", stability="unstable")
        answers = iter(["bad", "-1", "1.5", "999", "9" * 5000, "0002"])
        with redirect_stdout(io.StringIO()):
            selected, number = MODULE.select_configuration(self.book, "C1", reader=lambda: next(answers))
        self.assertEqual((selected, number), ("C2", 2))
        self.assertEqual(MODULE.select_configuration(self.book, "C1", reader=lambda: ""), ("C1", None))
        self.assertEqual(self.book.configurations[selected]["sysctl"]["net.ipv4.tcp_congestion_control"], "cubic")
        output = io.StringIO()
        with redirect_stdout(output):
            MODULE.measurement_table(self.book, "C1")
        self.assertIn("推荐", output.getvalue())
        self.assertIn("已回退", output.getvalue())
        self.assertIn("不稳定", output.getvalue())
        self.assertNotIn("C1", output.getvalue())
        self.assertNotIn("C2", output.getvalue())

    def test_yes_never_reads_input(self):
        reader = mock.Mock(side_effect=AssertionError("不应询问"))
        with redirect_stdout(io.StringIO()):
            self.book.measure_group("初值测速")
            self.assertEqual(MODULE.select_configuration(self.book, "C1", automatic=True, reader=reader), ("C1", None))
        reader.assert_not_called()

    def test_zero_selects_no_change_without_measured_original_configuration(self):
        self.book.register()
        for answer in ("0", "000", " 0 "):
            self.assertEqual(MODULE.select_configuration(self.book, "C1", reader=lambda: answer),
                             (None, 0))

    def test_zero_restores_original_files_and_buffers_below_trial_floor(self):
        original = self.book.register()
        before = copy.deepcopy(self.current)
        self.current = snapshot(24)
        with redirect_stdout(io.StringIO()):
            recommendation = self.book.measure_group("候选")[0]["config_id"]
        result = {"original_config": original, "buffer_plan": {"min_bytes": 20 * MODULE.MIB}}

        def restore(saved):
            self.current = copy.deepcopy(saved)

        with mock.patch.object(MODULE, "select_configuration", return_value=(None, 0)), \
                mock.patch.object(MODULE.Snapshot, "restore", side_effect=restore) as restore_call, \
                mock.patch.object(MODULE, "persist_selected_configuration") as persist, \
                redirect_stdout(io.StringIO()):
            final = MODULE.save_configuration_choice(self.book, self.worker, result,
                                                       recommendation, False, lambda: None)
        self.assertEqual(final, before)
        restore_call.assert_called_once_with(before)
        persist.assert_not_called()
        self.assertEqual(result["selected_number"], 0)

    def test_same_parameters_merge_repeats_and_modes_with_separate_snapshots(self):
        with redirect_stdout(io.StringIO()):
            self.book.measure_group("候选", modes=(1, 4))
            self.current["files"]["persisted.conf"] = {"data": "c2F2ZWQ="}
            self.current["service_active"] = True
            final = self.book.measure_group("队列验证", modes=(1, 4))
        self.assertEqual(len(self.book.configurations), 2)
        recommended = final[0]["config_id"]
        output = io.StringIO()
        with redirect_stdout(output):
            groups = MODULE.measurement_table(self.book, recommended)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["measurements"]), 8)
        self.assertEqual(len(groups[0]["config_ids"]), 2)
        self.assertEqual(len([line for line in output.getvalue().splitlines() if " | " in line]), 2)
        self.assertIn("100.00 / 0.010 | 200.00 / 0.010", output.getvalue())
        self.assertEqual(MODULE.select_configuration(self.book, recommended, reader=lambda: "1"), (recommended, 1))

    def test_equal_buffer_limits_do_not_merge_different_tcp_or_queue_parameters(self):
        with redirect_stdout(io.StringIO()):
            self.book.measure_group("初值")
            self.current["sysctl"]["net.ipv4.tcp_congestion_control"] = "cubic"
            self.book.measure_group("不同拥塞控制")
            self.current["queue"]["rate"] = 500
            self.book.measure_group("不同限速")
        self.assertEqual(len(MODULE.configuration_groups(self.book)), 3)

    def test_configuration_without_valid_measurements_cannot_be_selected(self):
        with redirect_stdout(io.StringIO()):
            initial = self.book.measure_group("初值")
        self.current = snapshot(14)
        self.worker.run.side_effect = MODULE.TaskError("测速失败")
        with self.assertRaises(MODULE.TaskError):
            self.book.measure_group("失败候选")
        recommendation = initial[0]["config_id"]
        answers = iter(["2", "1"])
        output = io.StringIO()
        with redirect_stdout(output):
            MODULE.measurement_table(self.book, recommendation)
            selected = MODULE.select_configuration(self.book, recommendation, reader=lambda: next(answers))
        self.assertEqual(selected, (recommendation, 1))
        self.assertIn("未取得 / 未取得", output.getvalue())
        self.assertIn("请输入表中可选的配置序号", output.getvalue())

    def test_unmeasured_rollback_recommendation_gets_a_selectable_number(self):
        original = self.book.register(snapshot(8, "cubic"))
        with redirect_stdout(io.StringIO()):
            self.book.measure_group("初值")
            groups = MODULE.measurement_table(self.book, original)
        self.assertEqual([group["number"] for group in groups], [1, 2])
        self.assertEqual(MODULE.configuration_verdict(groups[1], original), "推荐 / 未测速")
        self.assertEqual(MODULE.select_configuration(self.book, original, reader=lambda: "2"), (original, 2))

    def test_obvious_speed_and_retransmission_spread_and_missing_metrics_are_invalid(self):
        for field, value in (("receiver_mbps", 40), ("estimated_retrans_pct", 0.8),
                             ("receiver_mbps", None), ("estimated_retrans_pct", None),
                             ("estimated_retrans_pct", float("nan"))):
            with self.subTest(field=field, value=value):
                measured = rows()
                measured[0][field] = value
                self.assertTrue(MODULE.measurement_issues(measured, 1))
                self.assertFalse(MODULE.base_decision(None, rows(), measured)[0])
        self.assertTrue(MODULE.measurement_issues(rows()[:1], 1))
        self.assertFalse(MODULE.measurement_issues(rows(retrans=0), 1))

    def test_manual_save_persists_full_sysctl_and_removes_stale_shaping(self):
        selected = snapshot(14, "cubic")
        name = MODULE.CONFIG_FILES[0]
        content = "# 保留说明\nnet.ipv4.tcp_congestion_control = bbr\nother.setting = 7\n"
        selected["files"][name] = {"data": base64.b64encode(content.encode()).decode(),
                                   "mode": 0o640, "uid": 0, "gid": 0}
        self.worker.run.side_effect = None
        with mock.patch.object(MODULE.Snapshot, "restore_files") as persist:
            MODULE.persist_selected_configuration(self.worker, selected)
        entry = persist.call_args.args[0][name]
        actual = base64.b64decode(entry["data"]).decode("utf-8")
        self.assertIn("other.setting = 7", actual)
        self.assertIn("net.ipv4.tcp_congestion_control = cubic", actual)
        self.assertIn("net.core.rmem_max = " + str(14 * MODULE.MIB), actual)
        self.assertEqual(entry["mode"], 0o640)
        self.worker.run.assert_called_once_with("shape", "off")


class RawFailureTests(unittest.TestCase):
    def test_timeout_still_saves_partial_server_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "measurements").mkdir()
            coordinator = MODULE.Coordinator(types.SimpleNamespace(token_ttl=600), path, path, mock.Mock())
            server_path = path / "server.json"
            MODULE.atomic_json(server_path, {"start": {}, "intervals": []})
            coordinator.active = {"id": "timeout", "stage": "试调", "streams": 4, "duration": 10,
                                  "raw_path": server_path, "latency": {},
                                  "process": mock.Mock(wait=mock.Mock(side_effect=subprocess.TimeoutExpired("iperf3", 10)))}
            with self.assertRaises(MODULE.TaskError):
                coordinator.result("timeout", '{"end":')
            saved = json.loads((path / "measurements" / "timeout.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["raw_server"], {"start": {}, "intervals": []})
            self.assertIn("unparsed", saved["raw_client"])
            self.assertEqual(saved["status"], "failed")

    def test_missing_retransmission_saves_both_raw_documents_without_inventing_zero(self):
        from test_return import iperf_result
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "measurements").mkdir()
            coordinator = MODULE.Coordinator(types.SimpleNamespace(token_ttl=600), path, path, mock.Mock())
            raw = iperf_result()
            raw["end"]["sum_sent"].pop("retransmits")
            raw["start"]["cookie"] = "temporary"
            server_path = path / "server.json"
            MODULE.atomic_json(server_path, raw)
            coordinator.active = {"id": "sample", "stage": "试调", "streams": 1, "duration": 10,
                                  "raw_path": server_path, "process": mock.Mock(wait=mock.Mock(return_value=0)),
                                  "latency": {"idle": {}, "loaded": {}}}
            MODULE.atomic_json(path / "measurement-context.json", {"number": 7, "config_id": "C2"})
            with self.assertRaises(MODULE.TaskError):
                coordinator.result("sample", json.dumps(raw))
            saved = json.loads((path / "measurements" / "sample.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["number"], 7)
            self.assertEqual(saved["config_id"], "C2")
            self.assertEqual(saved["status"], "failed")
            self.assertNotIn("estimated_retrans_pct", saved)
            self.assertNotIn("cookie", saved["raw_server"]["start"])
            self.assertNotIn("retransmits", saved["raw_server"]["end"]["sum_sent"])
            self.assertEqual(coordinator.results, [])

    def test_malformed_json_preserves_diagnostic_text_without_cookie(self):
        value = MODULE.raw_document('{"start":{"cookie":"temporary"},"end":')
        self.assertIn("unparsed", value)
        self.assertNotIn("temporary", value["unparsed"])
        self.assertEqual(MODULE.raw_document('{"value": NaN}')["value"], "nan")

    def test_python_cli_validates_repeat_counts_before_starting_task(self):
        command = [str(ROOT / "tcpfit-return.py"), "run", "--script", "main",
                   "--client-script", "client", "--server", "server"]
        for options, expected in (([], 2), (["--repeats", "1"], 1), (["--repeats", "10"], 10)):
            with self.subTest(expected=expected), mock.patch.object(sys, "argv", command + options), \
                    mock.patch.object(MODULE, "run_task", return_value=0) as task:
                self.assertEqual(MODULE.main(), 0)
                task.assert_called_once()
                self.assertEqual(task.call_args[0][0].repeats, expected)
        for value in ("0", "11", "-1", "2.5", "bad", ""):
            result = subprocess.run([sys.executable, *command, "--repeats", value],
                                    capture_output=True, text=True, encoding="utf-8", timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertIn("--repeats", result.stderr)
            self.assertNotIn("root", result.stderr)


if __name__ == "__main__":
    unittest.main()
