"""防火墙选择和依赖准备回归；不调用真实防火墙或包管理器。"""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("tcpfit_return_firewall", ROOT / "tcpfit-return.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FirewallSelectionTests(unittest.TestCase):
    def select(self, available, family=4, ufw="inactive", legacy=False, rules=""):
        def run(args, **kwargs):
            if args == ["ufw", "status"]:
                output = "Status: " + ufw + "\n"
            elif args[-1] == "--version":
                output = "iptables v1.8.9 ({})\n".format("legacy" if legacy else "nf_tables")
            elif args[-1] == "-S":
                output = rules
            else:
                self.fail("选择工具时不应修改规则: " + repr(args))
            return subprocess.CompletedProcess(args, 0, output, "")
        with mock.patch.object(MODULE.shutil, "which", side_effect=lambda name: "/usr/bin/" + name if name in available else None), \
                mock.patch.object(MODULE, "command", side_effect=run):
            return MODULE.Firewall.select(family)

    def test_active_ufw_reuses_its_existing_backend(self):
        choice = self.select({"ufw", "nft", "iptables"}, ufw="active")
        self.assertEqual(choice, {"backend": "iptables", "binary": "iptables", "manager": "ufw"})

    def test_inactive_ufw_is_not_enabled(self):
        self.assertEqual(self.select({"ufw", "nft", "iptables"})["backend"], "nft")
        self.assertEqual(self.select({"ufw"})["backend"], "none")

    def test_nft_only_does_not_require_iptables(self):
        self.assertEqual(self.select({"nft"})["backend"], "nft")
        self.assertEqual(self.select({"nft"}, family=6)["backend"], "nft")

    def test_iptables_only_uses_the_requested_family(self):
        self.assertEqual(self.select({"iptables"})["binary"], "iptables")
        self.assertEqual(self.select({"ip6tables"}, family=6)["binary"], "ip6tables")
        self.assertEqual(self.select({"iptables"}, family=6)["backend"], "none")

    def test_active_legacy_rules_are_not_skipped(self):
        choice = self.select({"iptables", "nft"}, legacy=True, rules="-P INPUT DROP\n")
        self.assertEqual(choice["backend"], "iptables")
        choice = self.select({"iptables", "nft"}, legacy=True, rules="-P INPUT ACCEPT\n-A INPUT -p tcp -j DROP\n")
        self.assertEqual(choice["backend"], "iptables")
        choice = self.select({"iptables", "nft"}, legacy=True, rules="-P INPUT ACCEPT\n")
        self.assertEqual(choice["backend"], "nft")

    def test_no_tools_pairing_and_cleanup_do_not_run_system_commands(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(MODULE.shutil, "which", return_value=None), \
                mock.patch.object(MODULE, "command") as run:
            firewall = MODULE.Firewall(directory, 4, 5211, 5212, "unit-test")
            firewall.setup()
            firewall.pair("192.0.2.1")
            self.assertEqual(MODULE.read_json(firewall.path)["backend"], "none")
            self.assertEqual(firewall.state["peer"], "192.0.2.1")
            MODULE.Firewall.cleanup(firewall.path)
            self.assertFalse(firewall.path.exists())
            run.assert_not_called()

    def test_existing_firewall_read_failure_is_reported(self):
        with mock.patch.object(MODULE.shutil, "which", return_value="/usr/sbin/tool"), \
                mock.patch.object(MODULE, "command", return_value=subprocess.CompletedProcess([], 1, "", "permission denied")):
            with self.assertRaisesRegex(MODULE.TaskError, "UFW 状态读取失败"):
                MODULE.Firewall.select(4)
            with self.assertRaisesRegex(MODULE.TaskError, "nftables 规则读取失败"):
                MODULE.Firewall.nft_rules()


class DependencyTests(unittest.TestCase):
    def test_only_measurement_dependencies_are_requested_from_apt(self):
        # 替换包管理器和工具检测，验证真实准备函数的安装请求，不执行安装。
        stubs = '''
command(){
  if [ "$1" = -v ]; then
    [ "$2" = apt-get ]; return
  fi
  builtin command "$@"
}
id(){ printf '0\\n'; }
apt-get(){ printf 'APT'; printf ' <%s>' "$@"; printf '\\n'; }
'''
        for name, prepare, packages in (("tcpfit.sh", "IP_FAMILY=-6; return_dependencies", ("python3", "iperf3", "curl")),
                                        ("tcpfit-client.sh", "prepare", ("iperf3", "curl"))):
            with self.subTest(script=name):
                source = 'source "$1"' if name == "tcpfit.sh" else 'source "$1" --help'
                result = subprocess.run([shutil.which("bash") or "bash", "-c", source + "\n" + stubs + prepare,
                                         "test-dependencies", (ROOT / name).as_posix()], capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                installed = next(line for line in result.stdout.splitlines() if line.startswith("APT <install>"))
                self.assertIn("<--no-install-recommends>", installed)
                for package in packages:
                    self.assertIn("<" + package + ">", installed)
                for package in ("iptables", "nftables", "ufw", "openssl", "ca-certificates", "ca-bundle"):
                    self.assertNotIn("<" + package + ">", installed)


if __name__ == "__main__":
    unittest.main()
