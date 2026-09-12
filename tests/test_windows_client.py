"""Windows 原生客户端回归：真实 HTTP 和子进程，测速及安装器使用临时替代程序。"""
import base64
import ctypes
from ctypes import wintypes
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("tcpfit_return_windows", ROOT / "tcpfit-return.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

# 替代程序只在临时目录中运行，不产生测速流量，不调用包管理器。
FAKE_TOOL = r'''
using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;
class TestTool {
    static int Main(string[] args) {
        Console.OutputEncoding = new UTF8Encoding(false);
        if (args.Length == 1 && args[0] == "--version") {
            Console.WriteLine("iperf 3.test");
            return 0;
        }
        if (Path.GetFileNameWithoutExtension(Environment.GetCommandLineArgs()[0]) == "winget") {
            string root = Environment.GetEnvironmentVariable("LOCALAPPDATA");
            File.WriteAllLines(Path.Combine(root, "install-args.txt"), args);
            string target = Path.Combine(root, "Microsoft", "WinGet", "Links");
            Directory.CreateDirectory(target);
            File.Copy(Environment.GetCommandLineArgs()[0], Path.Combine(target, "iperf3.exe"));
            return 0;
        }
        string marker = Environment.GetEnvironmentVariable("TCPFIT_TEST_PID_FILE");
        File.WriteAllText(marker, Process.GetCurrentProcess().Id.ToString());
        string mode = Environment.GetEnvironmentVariable("TCPFIT_TEST_MODE");
        if (mode == "fail") {
            Console.Error.WriteLine("test download failed");
            return 23;
        }
        Thread.Sleep(mode == "hang" ? 60000 : 1400);
        Console.WriteLine("{\"arguments\":\"" + String.Join(" ", args) + "\"}");
        return 0;
    }
}
'''


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(os.name == "nt", "需要 Windows 原生 PowerShell")
class WindowsPowerShell51Tests(unittest.TestCase):
    shell_name = "powershell.exe"

    @classmethod
    def setUpClass(cls):
        cls.shell = shutil.which(cls.shell_name)
        if not cls.shell:
            raise unittest.SkipTest("未安装 " + cls.shell_name)
        framework = Path(os.environ["SystemRoot"]) / "Microsoft.NET"
        candidates = [framework / name / "v4.0.30319" / "csc.exe" for name in ("Framework64", "Framework")]
        compiler = next((path for path in candidates if path.is_file()), None)
        if compiler is None:
            raise unittest.SkipTest("缺少用于构建临时替代程序的 .NET Framework 编译器")
        scratch = tempfile.TemporaryDirectory(prefix="tcpfit-windows-tools-")
        cls.addClassCleanup(scratch.cleanup)
        source = Path(scratch.name) / "test-tool.cs"
        source.write_text(FAKE_TOOL, encoding="utf-8")
        cls.fake_tool = Path(scratch.name) / "test-tool.exe"
        compiled = subprocess.run([str(compiler), "/nologo", "/target:exe", "/out:" + str(cls.fake_tool), str(source)],
                                  capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        if compiled.returncode:
            raise RuntimeError(compiled.stdout + compiled.stderr)

    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="tcpfit Windows test ")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.bin = self.root / "bin with spaces"
        self.bin.mkdir()
        self.binary = self.bin / "iperf3.exe"
        shutil.copyfile(self.fake_tool, self.binary)
        self.env = dict(os.environ, PATH=str(self.bin), LOCALAPPDATA=str(self.root), ProgramFiles=str(self.root),
                        TCPFIT_TEST_PID_FILE=str(self.root / "child.pid"), TCPFIT_TEST_MODE="normal")
        self.start_server()

    def start_server(self, family=4):
        self.args = types.SimpleNamespace(family=family, server="::1" if family == 6 else "127.0.0.1",
                                          control_port=0, iperf_port=5212, token_ttl=60,
                                          client_script=str(ROOT / "tcpfit-client.sh"))
        self.coordinator = MODULE.Coordinator(self.args, self.root, self.root, mock.Mock())
        self.coordinator.next_job = mock.Mock(return_value="DONE OK Windows 接入完成")
        self.coordinator.save_latency = mock.Mock()
        self.coordinator.result = mock.Mock()
        MODULE.start_http(self.coordinator)
        self.addCleanup(self.coordinator.close)

    def launch(self, code=None):
        code = code or MODULE.join_command(self.args, self.coordinator.token, "windows")
        code = "[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)\n$ErrorActionPreference = 'Stop'\n" + code
        encoded = base64.b64encode(code.encode("utf-16-le")).decode("ascii")
        process = subprocess.Popen([self.shell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace")
        self.addCleanup(self.stop_client, process)
        return process

    @staticmethod
    def stop_client(process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)

    def run_client(self, code=None):
        process = self.launch(code)
        output = process.communicate(timeout=25)[0]
        return process.returncode, output

    def test_download_pair_single_and_four_connections_with_latency(self):
        ids = [secrets.token_hex(8), secrets.token_hex(8)]
        self.coordinator.next_job.side_effect = ["RUN {} 2 1".format(ids[0]), "RUN {} 2 4".format(ids[1]),
                                                 "DONE OK Windows 接入完成"]
        code, output = self.run_client()
        self.assertEqual(code, 0, output)
        self.assertIn("Windows 接入完成", output)
        self.coordinator.firewall.pair.assert_called_once_with("127.0.0.1")
        self.assertEqual(self.coordinator.result.call_count, 2)
        for call, job_id, streams in zip(self.coordinator.result.call_args_list, ids, (1, 4)):
            self.assertEqual(call.args[0], job_id)
            self.assertEqual(json.loads(call.args[1])["arguments"].split(),
                             ["-4", "-c", "127.0.0.1", "-p", "5212", "-R", "-P", str(streams), "-t", "2", "-J"])
        self.assertEqual(self.coordinator.save_latency.call_count, 4)
        for call in self.coordinator.save_latency.call_args_list:
            latency = MODULE.latency_summary(call.args[2])
            self.assertGreaterEqual(latency["samples"], 3 if call.args[1] == "idle" else 1)
        self.assertIsNone(self.coordinator.error)

    def test_latency_only_job_does_not_start_a_download(self):
        job_id = secrets.token_hex(8)
        self.coordinator.next_job.side_effect = ["LATENCY " + job_id, "DONE OK 延迟采集完成"]
        code, output = self.run_client()
        self.assertEqual(code, 0, output)
        self.assertIn("延迟采集完成", output)
        self.coordinator.save_latency.assert_called_once()
        call = self.coordinator.save_latency.call_args
        self.assertEqual(call.args[:2], (job_id, "idle"))
        self.assertGreaterEqual(MODULE.latency_summary(call.args[2])["samples"], 3)
        self.coordinator.result.assert_not_called()
        self.assertFalse((self.root / "child.pid").exists())

    def test_malformed_latency_request_is_rejected_before_sampling(self):
        self.coordinator.next_job.return_value = "LATENCY invalid; command"
        code, output = self.run_client()
        self.assertNotEqual(code, 0, output)
        self.assertIn("无法识别调优端请求", self.coordinator.error)
        self.coordinator.save_latency.assert_not_called()
        self.assertFalse((self.root / "child.pid").exists())

    def test_ipv6_download_command_and_measurement(self):
        self.coordinator.close()
        self.start_server(family=6)
        self.coordinator.next_job.side_effect = ["RUN {} 2 1".format(secrets.token_hex(8)), "DONE OK IPv6 完成"]
        code, output = self.run_client()
        self.assertEqual(code, 0, output)
        self.coordinator.firewall.pair.assert_called_once_with("::1")
        arguments = json.loads(self.coordinator.result.call_args.args[1])["arguments"].split()
        self.assertEqual(arguments[:3], ["-6", "-c", "::1"])

    def test_direct_file_supports_explicit_binary_path_and_chinese(self):
        self.binary.rename(self.root / "explicit iperf3.exe")
        values = [ROOT / "tcpfit-client.ps1", self.args.server, self.args.control_port,
                  self.args.iperf_port, self.coordinator.token]
        code = "& " + " ".join(map(ps_quote, values)) + " -IperfPath " + ps_quote(self.root / "explicit iperf3.exe")
        status, output = self.run_client(code)
        self.assertEqual(status, 0, output)
        self.assertIn("Windows 接入完成", output)

    def test_missing_binary_uses_winget_and_finds_new_install_without_path_refresh(self):
        self.binary.rename(self.bin / "winget.exe")
        code, output = self.run_client()
        self.assertEqual(code, 0, output)
        install_args = (self.root / "install-args.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(install_args[:6], ["install", "--id", "ar51an.iPerf3", "--exact", "--source", "winget"])
        self.assertTrue((self.root / "Microsoft/WinGet/Links/iperf3.exe").is_file())
        self.coordinator.firewall.pair.assert_called_once()

    def test_missing_tools_stop_before_pairing(self):
        self.binary.unlink()
        code, output = self.run_client()
        self.assertNotEqual(code, 0, output)
        self.assertIn("未找到 iperf3.exe 或 WinGet", output)
        self.coordinator.firewall.pair.assert_not_called()

    def test_script_version_mismatch_is_rejected_before_pairing(self):
        with mock.patch.object(MODULE, "VERSION", "0.0.0"):
            code, output = self.run_client()
        self.assertNotEqual(code, 0, output)
        self.assertIn("测速端脚本版本与调优端不一致", output)
        self.coordinator.firewall.pair.assert_not_called()

    def test_remote_command_is_validated_and_failure_is_reported(self):
        self.coordinator.next_job.return_value = "RUN {} 2 1; invalid".format(secrets.token_hex(8))
        code, output = self.run_client()
        self.assertNotEqual(code, 0, output)
        self.assertIn("无法识别调优端请求", self.coordinator.error)
        self.assertFalse((self.root / "child.pid").exists())
        self.coordinator.result.assert_not_called()

    def test_iperf_failure_never_submits_successful_result(self):
        self.env["TCPFIT_TEST_MODE"] = "fail"
        self.coordinator.next_job.return_value = "RUN {} 2 1".format(secrets.token_hex(8))
        code, output = self.run_client()
        self.assertNotEqual(code, 0, output)
        self.assertIn("退出码 23", self.coordinator.error)
        self.coordinator.result.assert_not_called()

    def test_hard_stop_kills_child_and_releases_client_lock(self):
        self.env["TCPFIT_TEST_MODE"] = "hang"
        self.coordinator.next_job.return_value = "RUN {} 2 1".format(secrets.token_hex(8))
        join = MODULE.join_command(self.args, self.coordinator.token, "windows")
        process = self.launch(join)
        marker = self.root / "child.pid"
        deadline = time.monotonic() + 10
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        if not marker.exists():
            self.stop_client(process)
            self.fail("替代测速进程未启动")
        duplicate, output = self.run_client(join)
        self.assertNotEqual(duplicate, 0, output)
        self.assertIn("已有测速端任务", output)
        self.assertIsNone(self.coordinator.error)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        child = kernel.OpenProcess(0x100001, False, int(marker.read_text(encoding="utf-8")))
        self.assertTrue(child)
        try:
            process.kill()
            process.communicate(timeout=10)
            self.assertEqual(kernel.WaitForSingleObject(child, 5000), 0, "强制关闭后测速子进程仍存活")
        finally:
            if kernel.WaitForSingleObject(child, 0) != 0:
                kernel.TerminateProcess(child, 1)
            kernel.CloseHandle(child)
        self.coordinator.close()
        self.start_server()
        self.env["TCPFIT_TEST_MODE"] = "normal"
        code, output = self.run_client()
        self.assertEqual(code, 0, output)
        self.coordinator.firewall.pair.assert_called_once()


class WindowsPowerShell7Tests(WindowsPowerShell51Tests):
    shell_name = "pwsh.exe"


if __name__ == "__main__":
    unittest.main()
