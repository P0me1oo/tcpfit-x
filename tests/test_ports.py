"""用真实本地套接字验证自动选端口、服务接管及退出释放，不修改防火墙。"""
import http.client
import importlib.util
from pathlib import Path
import socket
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("tcpfit_return_ports", ROOT / "tcpfit-return.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PortReservationTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "验证 Linux TIME_WAIT 端口复用语义")
    def test_immediate_restart_with_time_wait(self):
        for family in (4, 6):
            for name in ("control_port", "iperf_port"):
                with self.subTest(family=family, port=name):
                    host = "127.0.0.1" if family == 4 else "::1"
                    with self.socket(family) as listener, self.socket(family) as client:
                        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        listener.bind((host, 0))
                        port = listener.getsockname()[1]
                        listener.listen(1)
                        client.settimeout(5)
                        client.connect((host, port))
                        connection, _ = listener.accept()
                        # 服务端先关闭，让服务端端口进入 TIME_WAIT。
                        connection.close()
                        self.assertEqual(client.recv(1), b"")
                    with self.socket(family) as probe:
                        with self.assertRaises(OSError):
                            probe.bind((host, port))
                    args = self.args(family)
                    setattr(args, name, port)
                    with MODULE.reserve_ports(args) as reservations:
                        self.assertEqual(getattr(args, name), port)
                        reservations[name].listen(1)

    @unittest.skipUnless(sys.platform.startswith("linux"), "验证 Linux 监听端口不可抢占")
    def test_reuse_does_not_take_over_live_listener(self):
        for family in (4, 6):
            with self.subTest(family=family), self.socket(family) as listener:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("0.0.0.0" if family == 4 else "::", 0))
                listener.listen(1)
                for name in ("control_port", "iperf_port"):
                    args = self.args(family)
                    setattr(args, name, listener.getsockname()[1])
                    with self.assertRaisesRegex(MODULE.TaskError, "已被占用"):
                        with MODULE.reserve_ports(args):
                            self.fail("不能抢占已启用地址复用的监听端口")

    def args(self, family=4, control=0, iperf=0):
        return types.SimpleNamespace(family=family, control_port=control, iperf_port=iperf,
                                     server="127.0.0.1" if family == 4 else "::1", token_ttl=60,
                                     client_script=str(ROOT / "tcpfit-client.sh"))

    def socket(self, family=4):
        sock = socket.socket(socket.AF_INET if family == 4 else socket.AF_INET6, socket.SOCK_STREAM)
        if family == 6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        return sock

    def assert_reserved(self, port, family=4):
        with self.socket(family) as sock:
            with self.assertRaises(OSError):
                sock.bind(("0.0.0.0" if family == 4 else "::", port))

    def assert_released(self, ports, family=4):
        for port in ports:
            with self.socket(family) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("0.0.0.0" if family == 4 else "::", port))
                sock.listen(1)

    def check_automatic(self, family):
        args = self.args(family)
        with MODULE.reserve_ports(args):
            ports = (args.control_port, args.iperf_port)
            self.assertNotEqual(*ports)
            for port in ports:
                self.assertTrue(1024 <= port <= 65535)
                self.assert_reserved(port, family)
        self.assert_released(ports, family)

    def test_automatic_ipv4_ports_are_distinct_reserved_and_released(self):
        self.check_automatic(4)

    def test_automatic_ipv6_ports_are_distinct_reserved_and_released(self):
        try:
            with self.socket(6) as probe:
                probe.bind(("::1", 0))
        except OSError:
            self.skipTest("本机未提供 IPv6 回环")
        self.check_automatic(6)

    def test_occupied_manual_port_is_rejected_without_stopping_its_owner(self):
        with self.socket() as existing:
            existing.bind(("0.0.0.0", 0))
            existing.listen(1)
            port = existing.getsockname()[1]
            for name in ("control_port", "iperf_port"):
                args = self.args()
                setattr(args, name, port)
                with self.subTest(name=name), self.assertRaisesRegex(MODULE.TaskError, "已被占用"):
                    with MODULE.reserve_ports(args):
                        self.fail("占用端口不应启动任务")
                self.assert_reserved(port)
                self.assertEqual(existing.getsockname()[1], port)

    def test_manual_port_is_preserved_when_other_port_is_automatic(self):
        for name in ("control_port", "iperf_port"):
            with self.subTest(name=name):
                with self.socket() as probe:
                    probe.bind(("0.0.0.0", 0))
                    fixed = probe.getsockname()[1]
                args = self.args()
                setattr(args, name, fixed)
                with MODULE.reserve_ports(args):
                    self.assertEqual(getattr(args, name), fixed)
                    self.assertNotEqual(args.control_port, args.iperf_port)
                    self.assert_reserved(args.control_port)
                    self.assert_reserved(args.iperf_port)
                self.assert_released((args.control_port, args.iperf_port))

    def test_invalid_and_duplicate_ports_are_rejected(self):
        for value in (-1, 1023, 65536, 1.5, True, "5211", None):
            for name in ("control_port", "iperf_port"):
                args = self.args()
                setattr(args, name, value)
                with self.subTest(name=name, value=value), self.assertRaises(MODULE.TaskError):
                    with MODULE.reserve_ports(args):
                        self.fail("无效端口不应启动任务")
        with self.assertRaisesRegex(MODULE.TaskError, "必须不同"):
            with MODULE.reserve_ports(self.args(control=5211, iperf=5211)):
                self.fail("相同端口不应启动任务")

    def test_partial_reservation_failure_releases_the_first_port(self):
        with self.socket() as first, self.socket() as occupied:
            first.bind(("0.0.0.0", 0))
            occupied.bind(("0.0.0.0", 0))
            occupied.listen(1)
            available = first.getsockname()[1]
            first.close()
            args = self.args(control=available, iperf=occupied.getsockname()[1])
            with self.assertRaisesRegex(MODULE.TaskError, "已被占用"):
                with MODULE.reserve_ports(args):
                    self.fail("第二个端口绑定失败应终止")
            self.assert_released((available,))
            self.assert_reserved(occupied.getsockname()[1])

    def test_interrupt_before_services_start_releases_both_reservations(self):
        args = self.args()
        with self.assertRaises(KeyboardInterrupt):
            with MODULE.reserve_ports(args):
                raise KeyboardInterrupt()
        self.assert_released((args.control_port, args.iperf_port))

    def test_http_adopts_reserved_socket_and_close_releases_both_ports(self):
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted), tempfile.TemporaryDirectory() as directory:
                args = self.args()
                coordinator = MODULE.Coordinator(args, directory, directory, mock.Mock())
                with MODULE.reserve_ports(args) as reservations:
                    original = reservations["control_port"]
                    coordinator.port_reservations = reservations
                    try:
                        MODULE.start_http(coordinator)
                        self.assertIs(coordinator.httpd.socket, original)
                        self.assert_reserved(args.control_port)
                        self.assert_reserved(args.iperf_port)
                        connection = http.client.HTTPConnection(args.server, args.control_port, timeout=5)
                        try:
                            connection.request("GET", "/join.sh")
                            response = connection.getresponse()
                            self.assertEqual(response.status, 200)
                            self.assertEqual(response.read(), (ROOT / "tcpfit-client.sh").read_bytes())
                        finally:
                            connection.close()
                        self.assertIn(MODULE.client_script_url("tcpfit-client.sh"),
                                      MODULE.join_command(args, coordinator.token, "linux"))
                        if interrupted:
                            raise KeyboardInterrupt()
                    except KeyboardInterrupt:
                        pass
                    finally:
                        coordinator.close()
                    self.assertIsNone(coordinator.token)
                    self.assertIsNone(coordinator.session)
                    self.assert_released((args.control_port, args.iperf_port))
                    coordinator.close()

    def test_http_start_failure_releases_both_ports(self):
        for phase in ("activate", "thread"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                args = self.args()
                coordinator = MODULE.Coordinator(args, directory, directory, mock.Mock())
                target, name = (MODULE.ControlServer, "server_activate") if phase == "activate" else (MODULE.threading.Thread, "start")
                with self.assertRaisesRegex(OSError, "模拟接入服务启动失败"):
                    with MODULE.reserve_ports(args) as reservations:
                        coordinator.port_reservations = reservations
                        try:
                            with mock.patch.object(target, name, side_effect=OSError("模拟接入服务启动失败")):
                                MODULE.start_http(coordinator)
                        finally:
                            coordinator.close()
                self.assert_released((args.control_port, args.iperf_port))


if __name__ == "__main__":
    unittest.main()
