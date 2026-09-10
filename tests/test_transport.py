"""通过真实 HTTP 请求检查接入脚本、一次性配对和版本隔离。"""
import http.client
import importlib.util
from pathlib import Path
import tempfile
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

    def request(self, method, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.args.control_port, timeout=5)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_plain_http_download_and_single_use_pairing(self):
        status, script = self.request("GET", "/join.sh")
        self.assertEqual(status, 200)
        self.assertEqual(script, (ROOT / "tcpfit-client.sh").read_bytes())
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])
        self.assertEqual(self.request("GET", "/next")[0], 403)
        headers = {"Authorization": "Pair " + self.coordinator.token, "X-Tcpfit-Version": MODULE.VERSION}
        status, response = self.request("POST", "/pair", headers)
        self.assertEqual(status, 200)
        session = response.decode().strip()[3:]
        self.assertEqual(self.request("GET", "/next", {"Authorization": "Bearer " + session}), (200, b"WAIT\n"))
        self.assertEqual(self.request("POST", "/pair", headers)[0], 409)
        self.firewall.pair.assert_called_once_with("127.0.0.1")

    def test_old_protocol_is_rejected_before_pairing(self):
        headers = {"Authorization": "Pair " + self.coordinator.token, "X-Tcpfit-Version": "0.6.0"}
        self.assertEqual(self.request("POST", "/pair", headers)[0], 409)
        self.assertIsNone(self.coordinator.peer)
        self.firewall.pair.assert_not_called()

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
