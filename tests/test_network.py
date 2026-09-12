"""隔离网络验证：现有防火墙共存、无防火墙接入和 HTTP 下载。"""
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("tcpfit_return_network", ROOT / "tcpfit-return.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
ENABLED = sys.platform.startswith("linux") and os.environ.get("TCPFIT_LINUX_INTEGRATION") == "1"


@unittest.skipUnless(ENABLED, "仅在显式启用的 Linux 隔离网络中运行")
class NetworkIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.readlink("/proc/self/ns/net") == os.readlink("/proc/1/ns/net"):
            raise RuntimeError("必须使用 unshare --net，禁止在主机网络中测试")
        MODULE.command(["ip", "link", "set", "lo", "up"])

    def normalized_nft(self):
        def clean(value):
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items() if key not in ("handle", "metainfo", "packets", "bytes")}
            if isinstance(value, list):
                return [clean(item) for item in value if not isinstance(item, dict) or "metainfo" not in item]
            return value
        return clean(json.loads(MODULE.command(["nft", "-s", "-j", "list", "ruleset"]).stdout))

    @staticmethod
    def iptables_rules(family=4):
        binary = "iptables-save" if family == 4 else "ip6tables-save"
        return [line for line in MODULE.command([binary]).stdout.splitlines() if line.startswith("-A ") or "TFRET_" in line]

    def exercise_filter(self, backend, family=4, existing_drop=False, reload_ufw=False, legacy_record=False):
        binary = "iptables" if family == 4 else "ip6tables"
        if not shutil.which("nft" if backend == "nft" else binary):
            self.skipTest("缺少本用例需要的已有防火墙工具")
        af = socket.AF_INET if family == 4 else socket.AF_INET6
        target, peer, stranger = ("127.0.0.1", "127.0.0.2", "127.0.0.3") if family == 4 else ("::1", "::2", "::3")
        existing = "tcpfit_test_" + secrets.token_hex(4)
        existing_iptables = None
        if family == 6:
            for address in (peer, stranger):
                MODULE.command(["ip", "-6", "addr", "add", address + "/128", "dev", "lo", "nodad"])
        try:
            with tempfile.TemporaryDirectory() as directory, socket.socket(af) as listener, socket.socket(af) as control, socket.socket(af) as unrelated:
                listener.bind(("0.0.0.0" if family == 4 else "::", 0))
                listener.listen(20)
                control.bind(("0.0.0.0" if family == 4 else "::", 0))
                control.listen(20)
                unrelated.bind((target, 0))
                unrelated.listen(20)
                port, other_port = listener.getsockname()[1], unrelated.getsockname()[1]
                control_port = control.getsockname()[1]
                if existing_drop and backend == "nft":
                    MODULE.command(["nft", "-f", "-"], input_data=(
                        "add table inet {table}\n"
                        "add chain inet {table} input {{ type filter hook input priority 0; policy drop; }}\n"
                        "add rule inet {table} input ct state established,related accept\n"
                        "add rule inet {table} input tcp dport {port} accept\n"
                    ).format(table=existing, port=other_port))
                elif existing_drop:
                    existing_iptables = ["INPUT", "-p", "tcp", "-m", "multiport", "--dports", "{},{}".format(control_port, port),
                                         "-m", "comment", "--comment", existing, "-j", "DROP"]
                    MODULE.command([binary, "-I"] + existing_iptables)
                original_nft = self.normalized_nft() if backend == "nft" else None
                original_iptables = self.iptables_rules(family) if backend == "iptables" else None
                firewall = MODULE.Firewall(directory, family, control_port, port, secrets.token_hex(8))
                def connect(source, destination_port=port):
                    with socket.socket(af) as client:
                        client.settimeout(0.3)
                        client.bind((source, 0))
                        return client.connect_ex((target, destination_port)) == 0
                choice = {"backend": backend, "binary": binary, "manager": "ufw" if reload_ufw else None}
                try:
                    if existing_drop:
                        self.assertFalse(connect(peer, control_port))
                        self.assertFalse(connect(peer))
                    if reload_ufw:
                        firewall.setup()
                        self.assertEqual(firewall.state["manager"], "ufw")
                    else:
                        with mock.patch.object(MODULE.Firewall, "select", return_value=choice):
                            firewall.setup()
                    self.assertTrue(connect(peer, control_port))
                    self.assertFalse(connect(peer))
                    firewall.pair(peer)
                    self.assertTrue(connect(peer))
                    self.assertFalse(connect(stranger))
                    self.assertTrue(connect(stranger, other_port))
                    if reload_ufw:
                        MODULE.command(["ufw", "reload"])
                        self.assertTrue(connect(peer, control_port))
                        self.assertTrue(connect(peer))
                        self.assertFalse(connect(stranger))
                    if legacy_record:
                        state = MODULE.read_json(firewall.path)
                        for field in ("backend", "manager", "table"):
                            state.pop(field)
                        MODULE.atomic_json(firewall.path, state)
                finally:
                    MODULE.Firewall.cleanup(firewall.path)
                self.assertFalse(firewall.path.exists())
                if existing_drop:
                    self.assertFalse(connect(peer, control_port))
                    self.assertFalse(connect(peer))
                if backend == "nft":
                    self.assertEqual(self.normalized_nft(), original_nft)
                else:
                    self.assertEqual(self.iptables_rules(family), original_iptables)
        finally:
            if existing_drop and backend == "nft":
                MODULE.command(["nft", "delete", "table", "inet", existing], check=False)
            if existing_iptables:
                MODULE.command([binary, "-D"] + existing_iptables, check=False)
            if family == 6:
                for address in (peer, stranger):
                    MODULE.command(["ip", "-6", "addr", "del", address + "/128", "dev", "lo"], check=False)

    def test_nft_ipv4_and_ipv6_preserve_existing_drop_rules(self):
        for family in (4, 6):
            with self.subTest(family=family):
                self.exercise_filter("nft", family, existing_drop=True)

    def test_iptables_ipv4_and_ipv6_filter_without_nft_command(self):
        existing_which = shutil.which
        with mock.patch.object(MODULE.shutil, "which", side_effect=lambda name: None if name == "nft" else existing_which(name)):
            for family in (4, 6):
                with self.subTest(family=family):
                    self.exercise_filter("iptables", family, existing_drop=True)

    def test_old_firewall_record_still_cleans_only_its_rules(self):
        self.exercise_filter("iptables", legacy_record=True)

    def test_http_join_download_with_curl_or_wget_only(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            script = directory / "join.sh"
            script.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
            args = types.SimpleNamespace(family=4, server="127.0.0.1", control_port=0, iperf_port=45212,
                                         token_ttl=60, client_script=str(script))
            coordinator = MODULE.Coordinator(args, directory, directory, mock.Mock())
            MODULE.start_http(coordinator)
            args.control_port = coordinator.httpd.server_address[1]
            try:
                for downloader in ("curl", "wget"):
                    with self.subTest(downloader=downloader):
                        self.assertIsNotNone(shutil.which(downloader), "本用例需要已有的 " + downloader)
                        tools = directory / downloader
                        tools.mkdir()
                        (tools / "sh").symlink_to(shutil.which("sh"))
                        (tools / downloader).symlink_to(shutil.which(downloader))
                        result = subprocess.run(["/bin/sh", "-c", MODULE.join_command(args, coordinator.token)],
                                                env=dict(os.environ, PATH=str(tools), http_proxy="", HTTP_PROXY=""),
                                                capture_output=True, text=True, timeout=10)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout.splitlines(), [args.server, str(args.control_port), "45212", coordinator.token])
            finally:
                coordinator.close()

    def test_no_firewall_completes_reverse_single_and_four_stream_tests(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as reservation:
            directory = Path(directory)
            runtime = directory / "server"
            runtime.mkdir()
            (runtime / "measurements").mkdir()
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
            reservation.close()
            args = types.SimpleNamespace(family=4, server="127.0.0.1", control_port=0, iperf_port=port,
                                         token_ttl=60, client_script=str(ROOT / "tcpfit-client.sh"))
            firewall = MODULE.Firewall(runtime, 4, 0, port, secrets.token_hex(8))
            with mock.patch.object(MODULE.shutil, "which", return_value=None):
                firewall.setup()
            coordinator = MODULE.Coordinator(args, runtime, runtime, firewall)
            MODULE.start_http(coordinator)
            args.control_port = coordinator.httpd.server_address[1]
            client = subprocess.Popen(["sh", args.client_script, args.server, str(args.control_port), str(port), coordinator.token],
                                      env=dict(os.environ, TMPDIR=str(directory)), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                deadline = time.monotonic() + 10
                while not coordinator.paired.wait(0.1) and time.monotonic() < deadline:
                    self.assertIsNone(client.poll())
                self.assertTrue(coordinator.paired.is_set())
                idle = coordinator.measure_idle_latency(2)
                self.assertTrue(all(sample["mean_ms"] is not None for sample in idle))
                self.assertEqual(coordinator.results, [])
                for streams in (1, 4):
                    MODULE.request_measurement(types.SimpleNamespace(run_dir=str(runtime), duration=1, streams=streams, stage="HTTP 协议验证"))
                    measured = coordinator.results[-1]
                    self.assertEqual(measured["streams"], streams)
                    self.assertGreater(measured["receiver_mbps"], 0)
                    self.assertGreaterEqual(measured["retransmits"], 0)
                coordinator.finished = "OK HTTP 协议验证通过"
                output, _ = client.communicate(timeout=10)
                self.assertEqual(client.returncode, 0, output)
                self.assertEqual(len(coordinator.results), 2)
                self.assertFalse(list(directory.glob("tcpfit-client*")))
            finally:
                if client.poll() is None:
                    client.terminate()
                    client.communicate(timeout=10)
                coordinator.close()
                MODULE.Firewall.cleanup(firewall.path)

    def test_ufw_reload_and_cleanup_preserve_its_configuration(self):
        if not shutil.which("ufw"):
            self.skipTest("没有已有的 UFW，测试不会安装")
        if os.readlink("/proc/self/ns/mnt") == os.readlink("/proc/1/ns/mnt"):
            self.skipTest("UFW 共存测试还需要 unshare --mount")
        MODULE.command(["mount", "--make-rprivate", "/"])
        mounted = []
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            # UFW 所有可写配置和运行目录都绑定到副本；原文件和主机规则均不参与测试。
            shutil.copytree("/etc/ufw", directory / "ufw")
            shutil.copy2("/etc/default/ufw", directory / "default")
            (directory / "run").mkdir()
            (directory / "run" / "lock").mkdir()
            defaults = (directory / "default").read_text(encoding="utf-8")
            defaults += '\nIPT_SYSCTL="/dev/null"\n'
            (directory / "default").write_text(defaults, encoding="utf-8")
            try:
                for source, destination in ((directory / "ufw", "/etc/ufw"), (directory / "default", "/etc/default/ufw"), (directory / "run", "/run")):
                    MODULE.command(["mount", "--bind", source, destination])
                    mounted.append(destination)
                MODULE.command(["ufw", "--force", "reset"])
                MODULE.command(["ufw", "--force", "enable"])
                MODULE.command(["ufw", "allow", "12345/tcp", "comment", "保留原有规则"])
                saved = {path.name: path.read_bytes() for path in Path("/etc/ufw").glob("*") if path.is_file()}
                self.exercise_filter("iptables", reload_ufw=True)
                self.assertEqual(saved, {path.name: path.read_bytes() for path in Path("/etc/ufw").glob("*") if path.is_file()})
            finally:
                if "/run" in mounted:
                    MODULE.command(["ufw", "--force", "disable"], check=False)
                for destination in reversed(mounted):
                    MODULE.command(["umount", destination])


if __name__ == "__main__":
    unittest.main()
