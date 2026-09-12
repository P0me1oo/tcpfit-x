"""通过真实 HTTP 请求检查接入脚本、一次性配对和版本隔离。"""
import http.client
import importlib.util
from pathlib import Path
import shutil
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("tcpfit_return_transport", ROOT / "tcpfit-return.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class HttpTransportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.args = types.SimpleNamespace(family=4, server="127.0.0.1", control_port=0, iperf_port=5212,
                                          token_ttl=60, client_script=str(ROOT / "tcpfit-client.sh"))
        self.firewall = mock.Mock()
        self.coordinator = MODULE.Coordinator(self.args, self.directory.name, self.directory.name, self.firewall)
        MODULE.start_http(self.coordinator)
        self.args.control_port = self.coordinator.httpd.server_address[1]

    def tearDown(self):
        self.coordinator.close()
        self.directory.cleanup()

    def request(self, method, path, headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.args.control_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_plain_http_download_and_single_use_pairing(self):
        for extension in ("sh", "ps1"):
            with self.subTest(extension=extension):
                status, script = self.request("GET", "/join." + extension)
                self.assertEqual(status, 200)
                self.assertEqual(script, (ROOT / ("tcpfit-client." + extension)).read_bytes())
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])
        self.assertEqual(self.request("GET", "/next")[0], 403)
        headers = {"Authorization": "Pair " + self.coordinator.token, "X-Tcpfit-Version": MODULE.VERSION}
        status, response = self.request("POST", "/pair", headers)
        self.assertEqual(status, 200)
        session = response.decode().strip()[3:]
        self.assertEqual(self.request("GET", "/next", {"Authorization": "Bearer " + session}), (200, b"WAIT\n"))
        self.assertEqual(self.request("POST", "/pair", headers)[0], 409)
        self.firewall.pair.assert_called_once_with("127.0.0.1")

    def test_mismatched_version_is_rejected_before_pairing(self):
        headers = {"Authorization": "Pair " + self.coordinator.token, "X-Tcpfit-Version": "0.0.0"}
        self.assertEqual(self.request("POST", "/pair", headers)[0], 409)
        self.assertIsNone(self.coordinator.peer)
        self.firewall.pair.assert_not_called()

    def test_latency_only_requests_collect_samples_without_starting_iperf(self):
        status, response = self.request("POST", "/pair", {
            "Authorization": "Pair " + self.coordinator.token, "X-Tcpfit-Version": MODULE.VERSION})
        self.assertEqual(status, 200)
        headers = {"Authorization": "Bearer " + response.decode().strip()[3:]}
        results, errors = [], []

        def collect():
            try:
                results.extend(self.coordinator.measure_idle_latency(2))
            except Exception as error:
                errors.append(error)

        collector = threading.Thread(target=collect, daemon=True)
        with mock.patch.object(MODULE.subprocess, "Popen", side_effect=AssertionError("延迟采集不能启动 iperf3")):
            collector.start()
            try:
                for payload in ("0.10 0\n0.12 0\n0.14 0\n", "0 0\n"):
                    deadline = time.monotonic() + 5
                    while True:
                        status, response = self.request("GET", "/next", headers)
                        self.assertEqual(status, 200)
                        if response != b"WAIT\n" or time.monotonic() >= deadline:
                            break
                        time.sleep(0.05)
                    self.assertRegex(response.decode().strip(), r"^LATENCY [a-f0-9]{16}$")
                    job_id = response.decode().split()[1]
                    self.assertEqual(self.request("GET", "/next", headers), (200, b"WAIT\n"))
                    self.assertEqual(self.request("POST", "/latency/" + job_id + "/idle", headers, payload)[0], 200)
                collector.join(timeout=5)
                self.assertFalse(collector.is_alive())
                self.assertFalse(errors)
            finally:
                if collector.is_alive():
                    self.coordinator.fail("测试结束")
                    collector.join(timeout=5)
        self.assertEqual([row["sample"] for row in results], [1, 2])
        self.assertAlmostEqual(results[0]["mean_ms"], 120)
        self.assertIsNone(results[1]["mean_ms"])
        self.assertEqual(self.coordinator.results, [])
        self.assertEqual(MODULE.read_json(Path(self.directory.name) / "idle-latency.json"), results)
        self.assertFalse((Path(self.directory.name) / "request.json").exists())
        self.assertFalse(list(Path(self.directory.name).glob("result-*.json")))

    def test_latency_only_job_rejects_throughput_and_loaded_results_and_closes_cleanly(self):
        job_id = "0123456789abcdef"
        MODULE.atomic_json(Path(self.directory.name) / "request.json",
                           {"id": job_id, "kind": "latency", "stage": "空载延迟", "sample": 1})
        self.assertEqual(self.coordinator.next_job(), "LATENCY " + job_id)
        with self.assertRaisesRegex(MODULE.TaskError, "不接受吞吐"):
            self.coordinator.result(job_id, "{}")
        with self.assertRaisesRegex(MODULE.TaskError, "不能回报满载"):
            self.coordinator.save_latency(job_id, "loaded", "0.1 0")
        with mock.patch.object(MODULE, "stop_process") as stop:
            self.coordinator.close()
        stop.assert_not_called()

    def test_environment_requires_windows_module_with_matching_version(self):
        root = Path(self.directory.name)
        for name in ("tcpfit.sh", "tcpfit-client.sh", "tcpfit-client.ps1"):
            shutil.copyfile(ROOT / name, root / name)
        args = types.SimpleNamespace(family=4, server="127.0.0.1", control_port=5211, iperf_port=5212,
                                     token_ttl=60, repeats=1, server_bw=None, client_bw=1000,
                                     script=str(root / "tcpfit.sh"), client_script=str(root / "tcpfit-client.sh"))
        windows = root / "tcpfit-client.ps1"
        with mock.patch.object(MODULE.os, "geteuid", return_value=0, create=True), \
                mock.patch.object(MODULE.sys, "platform", "linux"), \
                mock.patch.object(MODULE.shutil, "which", return_value="test-binary"):
            MODULE.validate_environment(args)
            windows.unlink()
            with self.assertRaisesRegex(MODULE.TaskError, "缺少同版本程序文件.*tcpfit-client.ps1"):
                MODULE.validate_environment(args)
            windows.write_text("$TCPFIT_CLIENT_VERSION = '0.0.0'", encoding="utf-8-sig")
            with self.assertRaisesRegex(MODULE.TaskError, "调优模块版本不一致"):
                MODULE.validate_environment(args)

    def test_join_command_uses_direct_http_and_four_arguments(self):
        for family, server, host in ((4, "192.0.2.1", "192.0.2.1"), (6, "2001:db8::1", "[2001:db8::1]")):
            with self.subTest(family=family):
                args = types.SimpleNamespace(family=family, server=server, control_port=5211, iperf_port=5212)
                join = MODULE.join_command(args, self.coordinator.token)
                self.assertEqual(join.count("http://" + host + ":5211/join.sh"), 2)
                self.assertNotIn("https:", join)
                self.assertNotIn("pinnedpubkey", join)
                self.assertNotIn("github", join)
                self.assertTrue(join.endswith("sh -s -- {} 5211 5212 {}".format(server, self.coordinator.token)))


if __name__ == "__main__":
    unittest.main()
