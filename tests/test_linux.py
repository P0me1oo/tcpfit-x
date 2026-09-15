"""Linux 隔离集成测试：以 root 在 unshare --net 中显式执行，不触碰出口网卡。"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("tcpfit_return_linux", ROOT / "tcpfit-return.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
ENABLED = sys.platform.startswith("linux") and os.environ.get("TCPFIT_LINUX_INTEGRATION") == "1"


@unittest.skipUnless(ENABLED, "仅在显式启用的 Linux 隔离网络中运行")
class LinuxIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.readlink("/proc/self/ns/net") == os.readlink("/proc/1/ns/net"):
            raise RuntimeError("请使用 unshare --net，禁止在主机出口网络中运行这些测试")
        MODULE.command(["ip", "link", "set", "lo", "up"])
        MODULE.command(["ip", "link", "add", "tf-test0", "numtxqueues", "2", "type", "dummy"])
        MODULE.command(["ip", "link", "set", "tf-test0", "up"])

    @classmethod
    def tearDownClass(cls):
        MODULE.command(["ip", "link", "del", "tf-test0"], check=False)

    def setUp(self):
        MODULE.command(["tc", "qdisc", "del", "dev", "tf-test0", "root"], check=False)

    def normalized(self, state):
        state = copy.deepcopy(state)
        state.pop("raw_qdisc")
        state.pop("raw_class")
        state["qdiscs"].sort(key=lambda entry: entry["parent"])
        return state

    def check_roundtrip(self):
        before = MODULE.QueueState.capture("tf-test0")
        MODULE.command(["tc", "qdisc", "replace", "dev", "tf-test0", "root", "handle", "7:", "fq"])
        MODULE.QueueState.restore(before)
        after = MODULE.QueueState.capture("tf-test0")
        self.assertEqual(self.normalized(before), self.normalized(after))

    def test_htb_fq_roundtrip(self):
        MODULE.command(["tc", "qdisc", "replace", "dev", "tf-test0", "root", "handle", "1:", "htb", "default", "10"])
        MODULE.command(["tc", "class", "replace", "dev", "tf-test0", "parent", "1:", "classid", "1:10", "htb", "rate", "321mbit", "ceil", "321mbit", "burst", "160500", "cburst", "160500", "quantum", "1514"])
        MODULE.command(["tc", "qdisc", "replace", "dev", "tf-test0", "parent", "1:10", "handle", "10:", "fq", "limit", "40960", "flow_limit", "8192", "quantum", "3028", "maxrate", "321mbit"])
        self.check_roundtrip()

    def test_custom_fq_codel_roundtrip(self):
        MODULE.command(["tc", "qdisc", "replace", "dev", "tf-test0", "root", "handle", "2:", "fq_codel", "limit", "2000", "target", "8ms", "interval", "150ms", "noecn"])
        self.check_roundtrip()

    def test_custom_fq_rate_limit_roundtrip(self):
        MODULE.command(["tc", "qdisc", "replace", "dev", "tf-test0", "root", "handle", "2:", "fq", "limit", "4000", "flow_limit", "200", "quantum", "3028", "initial_quantum", "15140", "maxrate", "250mbit", "nopacing"])
        before = MODULE.QueueState.capture("tf-test0")
        self.assertTrue(before["limited_fq"])
        self.assertIsNone(before["rate"])
        self.check_roundtrip()

    def test_fq_bands_and_custom_weights_roundtrip(self):
        MODULE.command(["tc", "qdisc", "replace", "dev", "tf-test0", "root", "handle", "2:", "fq"])
        reported = json.loads(MODULE.command(["tc", "-j", "qdisc", "show", "dev", "tf-test0"]).stdout)[0]["options"]
        if "bands" not in reported:
            self.skipTest("当前内核或 tc 不提供 fq bands/weights")
        before = MODULE.QueueState.capture("tf-test0")
        options = before["qdiscs"][0]["options"]
        priomap = [2, 1, 0, 2, 1, 0, 2, 1, 0, 2, 1, 0, 2, 1, 0, 2]
        weights = [262144, 131072, 65536]
        index = options.index("priomap") + 1
        options[index:index + 16] = [str(value) for value in priomap]
        index = options.index("weights") + 1
        options[index:index + 3] = [str(value) for value in weights]
        MODULE.QueueState.restore(before)
        reported = json.loads(MODULE.command(["tc", "-j", "qdisc", "show", "dev", "tf-test0"]).stdout)[0]["options"]
        reported = {key.strip(): value for key, value in reported.items()}
        self.assertEqual(reported["bands"], 3)
        self.assertEqual(reported["priomap"], priomap)
        self.assertEqual(reported["weights"], weights)
        self.check_roundtrip()

    def test_mq_individual_leaves_roundtrip(self):
        MODULE.command(["tc", "qdisc", "replace", "dev", "tf-test0", "root", "handle", "1:", "mq"])
        MODULE.command(["tc", "qdisc", "replace", "dev", "tf-test0", "parent", "1:1", "handle", "11:", "fq", "limit", "4321"])
        MODULE.command(["tc", "qdisc", "replace", "dev", "tf-test0", "parent", "1:2", "handle", "12:", "fq_codel", "target", "8ms", "noecn"])
        self.check_roundtrip()

    @unittest.skipUnless(shutil.which("iptables-save"), "缺少已有的 iptables 工具")
    def test_firewall_only_allows_paired_source_and_cleans_own_rules(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            listener.bind(("0.0.0.0", 0))
            listener.listen(20)
            port = listener.getsockname()[1]
            firewall = MODULE.Firewall(directory, 4, port + 1, port, secrets.token_hex(8))
            original = MODULE.command(["iptables-save"]).stdout
            def connect(source):
                with socket.socket() as client:
                    client.settimeout(0.3)
                    client.bind((source, 0))
                    return client.connect_ex(("127.0.0.1", port)) == 0
            try:
                firewall.setup()
                self.assertFalse(connect("127.0.0.2"))
                firewall.pair("127.0.0.2")
                self.assertTrue(connect("127.0.0.2"))
                self.assertFalse(connect("127.0.0.3"))
            finally:
                MODULE.Firewall.cleanup(firewall.path)
            def rules(text):
                return [line for line in text.splitlines() if line.startswith("-A") or "TFRET_" in line]
            self.assertEqual(rules(original), rules(MODULE.command(["iptables-save"]).stdout))
            self.assertFalse(firewall.path.exists())

    def test_common_lock_rejects_original_entry_and_manual_restore(self):
        with MODULE.task_lock():
            original = subprocess.run(["bash", "-c", 'source "$1"; take_lock', "test-lock", str(ROOT / "tcpfit.sh")], capture_output=True, text=True)
            self.assertNotEqual(original.returncode, 0)
            self.assertIn("已有 tcpfit 任务", original.stdout + original.stderr)
            restore = subprocess.run([sys.executable, str(ROOT / "tcpfit-return.py"), "restore", "--snapshot", "/nonexistent"], capture_output=True, text=True)
            self.assertNotEqual(restore.returncode, 0)
            self.assertIn("已有 tcpfit 任务", restore.stderr)

    def test_sigint_releases_ports_while_waiting_and_during_measurement(self):
        if not shutil.which("iperf3"):
            self.skipTest("本用例需要已有的 iperf3")
        for measuring in (False, True):
            with self.subTest(measuring=measuring), tempfile.TemporaryDirectory() as directory:
                directory = Path(directory)
                fixture = directory / "interrupt.py"
                fixture.write_text('''import importlib.util, pathlib, secrets, signal, sys, time, types
spec = importlib.util.spec_from_file_location("ret", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
d = pathlib.Path(sys.argv[2]); (d / "measurements").mkdir()
args = types.SimpleNamespace(family=4, control_port=0, iperf_port=0, token_ttl=60)
with m.reserve_ports(args) as reservations:
    firewall = m.Firewall(d, 4, args.control_port, args.iperf_port, secrets.token_hex(8))
    coordinator = m.Coordinator(args, d, d, firewall)
    coordinator.port_reservations = reservations
    signal.signal(signal.SIGINT, lambda signum, frame: coordinator.fail("调优任务收到取消或断线信号"))
    try:
        firewall.setup()
        m.start_http(coordinator)
        if sys.argv[3] == "1":
            firewall.pair("127.0.0.1")
            m.atomic_json(d / "request.json", {"id": secrets.token_hex(8), "streams": 1, "duration": 10, "stage": "中断验证"})
            coordinator.next_job()
        m.atomic_json(d / "ready.json", {"control": args.control_port, "iperf": args.iperf_port})
        while True:
            coordinator.check()
            time.sleep(0.05)
    except m.TaskError:
        pass
    finally:
        coordinator.close()
        m.Firewall.cleanup(firewall.path)
''', encoding="utf-8")
                owner = subprocess.Popen([sys.executable, str(fixture), str(ROOT / "tcpfit-return.py"), str(directory), "1" if measuring else "0"],
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                try:
                    deadline = time.monotonic() + 10
                    while not (directory / "ready.json").exists() and time.monotonic() < deadline:
                        self.assertIsNone(owner.poll())
                        time.sleep(0.05)
                    self.assertTrue((directory / "ready.json").exists())
                    ports = MODULE.read_json(directory / "ready.json")
                    for port in ports.values():
                        with socket.socket() as probe:
                            with self.assertRaises(OSError):
                                probe.bind(("0.0.0.0", port))
                    process = MODULE.read_json(directory / "iperf.json") if measuring else None
                    owner.send_signal(signal.SIGINT)
                    output = owner.communicate(timeout=15)[0]
                    self.assertEqual(owner.returncode, 0, output)
                    self.assertIn("取消或断线信号", output)
                    for port in ports.values():
                        with socket.socket() as probe:
                            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                            probe.bind(("0.0.0.0", port))
                            probe.listen(1)
                    self.assertFalse((directory / "firewall.json").exists())
                    if process:
                        self.assertNotEqual(MODULE.process_stamp(process["pid"]), process["stamp"])
                finally:
                    if owner.poll() is None:
                        owner.kill()
                    owner.communicate(timeout=10)
                    if (directory / "iperf.json").exists():
                        MODULE.stop_process(MODULE.read_json(directory / "iperf.json"))
                    MODULE.Firewall.cleanup(directory / "firewall.json")

    def test_client_sigkill_revokes_credentials_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            runtime = directory / "server"
            runtime.mkdir()
            args = types.SimpleNamespace(family=4, control_port=0, token_ttl=60, client_script=str(ROOT / "tcpfit-client.sh"))
            coordinator = MODULE.Coordinator(args, runtime, runtime, mock.Mock())
            MODULE.start_http(coordinator)
            control_port = coordinator.httpd.server_address[1]
            env = dict(os.environ, TMPDIR=str(directory))
            client = subprocess.Popen(["sh", str(ROOT / "tcpfit-client.sh"), "-e", "127.0.0.1:{}".format(control_port),
                                       "-p", "45212", "-t", coordinator.token], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                deadline = time.monotonic() + 10
                while not coordinator.paired.is_set() and time.monotonic() < deadline:
                    self.assertIsNone(client.poll())
                    time.sleep(0.1)
                self.assertTrue(coordinator.paired.is_set())
                # 等测速端将一次性 token 换成会话凭据后，再模拟不可捕获的退出。
                while not list(directory.glob("tcpfit-client.*/curl.conf")) and time.monotonic() < deadline:
                    time.sleep(0.1)
                config = next(directory.glob("tcpfit-client.*/curl.conf"))
                while "Bearer " not in config.read_text() and time.monotonic() < deadline:
                    time.sleep(0.1)
                client.kill()
                client.wait(timeout=5)
                deadline = time.monotonic() + 8
                while list(directory.glob("tcpfit-client*")) and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertFalse(list(directory.glob("tcpfit-client*")))
                self.assertIn("测速端进程异常退出", coordinator.error or "")
            finally:
                if client.poll() is None:
                    client.terminate()
                client.communicate(timeout=10)
                coordinator.close()

    def test_ipv6_route_expiry_roundtrip(self):
        MODULE.command(["ip", "-6", "route", "add", "default", "dev", "tf-test0", "metric", "1024", "expires", "600"])
        try:
            before = MODULE.command(["ip", "-6", "route", "show", "default"]).stdout.strip()
            self.assertIn("expires", before)
            saved_at = time.time()
            MODULE.command(["ip", "-6", "route", "replace", "default", "dev", "tf-test0", "metric", "1024", "initcwnd", "32"])
            MODULE.command(["ip", "-6", "route", "replace"] + MODULE.Snapshot.route_restore_args(before, saved_at))
            after = MODULE.command(["ip", "-6", "route", "show", "default"]).stdout.strip()
            self.assertEqual(MODULE.Snapshot.route_config(before), MODULE.Snapshot.route_config(after))
        finally:
            MODULE.command(["ip", "-6", "route", "del", "default", "dev", "tf-test0", "metric", "1024"], check=False)

    def test_guard_restores_after_sigkill(self):
        MODULE.command(["tc", "qdisc", "replace", "dev", "tf-test0", "root", "handle", "2:", "fq_codel", "target", "8ms"])
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            run_dir = directory / "runtime"
            run_dir.mkdir()
            (directory / "bin").mkdir()
            systemctl = directory / "bin" / "systemctl"
            systemctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            systemctl.chmod(0o700)
            fixture = directory / "owner.py"
            fixture.write_text('''import importlib.util, os, pathlib, secrets, subprocess, sys, time
spec=importlib.util.spec_from_file_location("ret", sys.argv[1])
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
d=pathlib.Path(sys.argv[2]); run=d/"runtime"
with m.task_lock() as fd:
    state={"queue":m.QueueState.capture("tf-test0"), "sysctl":{}, "files":{}, "routes":{}, "service_active":False}
    m.atomic_json(d/"before.json", state)
    journal={"owner_pid":os.getpid(), "owner_stamp":m.process_stamp(os.getpid()), "record_dir":str(d), "run_dir":str(run), "state_dir":str(d), "dirty":True, "committed":False, "restored":False, "done":False}
    m.atomic_json(d/"transaction.json", journal)
    m.atomic_json(d/"return-pending.json", {"record_dir":str(d)})
    out=open(d/"guard.log", "w")
    subprocess.Popen([sys.executable, sys.argv[1], "guard", "--record-dir", str(d)], stdout=out, stderr=subprocess.STDOUT, pass_fds=(fd,))
    m.Firewall(d, 4, 45211, 45212, secrets.token_hex(8)).setup()
    m.command(["tc","qdisc","replace","dev","tf-test0","root","handle","7:","fq"])
    (run/"server.key").write_text("test-only-placeholder")
    (d/"ready").write_text("ready")
    time.sleep(60)
''', encoding="utf-8")
            env = dict(os.environ, PATH=str(directory / "bin") + os.pathsep + os.environ["PATH"])
            owner = subprocess.Popen([sys.executable, str(fixture), str(ROOT / "tcpfit-return.py"), str(directory)], env=env)
            try:
                deadline = time.monotonic() + 10
                while not (directory / "ready").exists() and time.monotonic() < deadline:
                    self.assertIsNone(owner.poll())
                    time.sleep(0.1)
                self.assertTrue((directory / "ready").exists())
                firewall_state = MODULE.read_json(directory / "firewall.json")
                owner.kill()
                owner.wait(timeout=5)
                deadline = time.monotonic() + 10
                while not MODULE.read_json(directory / "transaction.json")["done"] and time.monotonic() < deadline:
                    time.sleep(0.1)
                journal = MODULE.read_json(directory / "transaction.json")
                self.assertTrue(journal["done"], (directory / "guard.log").read_text())
                self.assertTrue(journal["restored"])
                self.assertFalse(run_dir.exists())
                self.assertFalse((directory / "return-pending.json").exists())
                self.assertFalse((directory / "firewall.json").exists())
                if firewall_state["backend"] == "nft":
                    self.assertFalse(any(entry.get("table", {}).get("name") == firewall_state["table"] for entry in MODULE.Firewall.nft_rules()))
                self.assertEqual(self.normalized(MODULE.read_json(directory / "before.json")["queue"]), self.normalized(MODULE.QueueState.capture("tf-test0")))
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
