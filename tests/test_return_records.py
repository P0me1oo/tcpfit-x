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
        self.assertEqual([row["streams"] for row in baseline], [1, 1])
        self.assertEqual([row["streams"] for row in trial], [4, 4])
        self.assertEqual(len(custom), 5)
        self.assertEqual(self.worker.run.call_count, 9)
        self.assertEqual([row["number"] for row in self.book.records], list(range(1, 10)))
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
            MODULE.measurement_table(self.book.records, "C1")
        lines = [line for line in output.getvalue().splitlines() if " | " in line]
        self.assertEqual(len(lines), 5)
        self.assertIn("失败", lines[2])
        self.assertIn("跳过", lines[3])
        self.assertIn("未取得 | 未取得", lines[2])

    def test_selection_accepts_rejected_and_unstable_valid_samples_and_rejects_invalid_numbers(self):
        with redirect_stdout(io.StringIO()):
            self.book.measure_group("基线")
            self.current = snapshot(14, cc="cubic")
            self.book.measure_group("回退候选", modes=(4,))
        self.book.records[-1].update(decision="已回退", stability="unstable")
        self.book.records[0]["status"] = "failed"
        answers = iter(["bad", "-1", "1.5", "999", "1", "9" * 5000, "0004"])
        with redirect_stdout(io.StringIO()):
            selected, number = MODULE.select_configuration(self.book, "C1", reader=lambda: next(answers))
        self.assertEqual((selected, number), ("C2", 4))
        self.assertEqual(MODULE.select_configuration(self.book, "C1", reader=lambda: ""), ("C1", None))
        self.assertEqual(self.book.configurations[selected]["sysctl"]["net.ipv4.tcp_congestion_control"], "cubic")
        output = io.StringIO()
        with redirect_stdout(output):
            MODULE.measurement_table(self.book.records, "C1")
        self.assertIn("自动推荐配置", output.getvalue())
        self.assertIn("已回退", output.getvalue())
        self.assertIn("不稳定", output.getvalue())

    def test_yes_never_reads_input(self):
        reader = mock.Mock(side_effect=AssertionError("不应询问"))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(MODULE.select_configuration(self.book, "C1", automatic=True, reader=reader), ("C1", None))
        reader.assert_not_called()

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

    def test_python_cli_rejects_invalid_repeat_counts_before_environment_checks(self):
        for value in ("1", "0", "11", "bad"):
            result = subprocess.run([sys.executable, str(ROOT / "tcpfit-return.py"), "run",
                                     "--script", "main", "--client-script", "client", "--server", "server",
                                     "--repeats", value], capture_output=True, text=True, encoding="utf-8", timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertIn("--repeats", result.stderr)
            self.assertNotIn("root", result.stderr)


if __name__ == "__main__":
    unittest.main()
