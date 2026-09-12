"""优化线路调优入口和数字菜单回归；替代系统命令，不访问外网或修改网络。"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") or "bash"

RETURN_STUBS = r'''
exec 3>&2
IP_FAMILY=-4
need_root(){ :; }
take_lock(){ :; }
have_ipv4(){ return 0; }
have_ipv6(){ return 1; }
return_dependencies(){ printf 'DEPENDENCIES\n'; }
return_assets(){ RETURN_MAIN=main; RETURN_HELPER=helper; RETURN_CLIENT=client; }
python3(){ printf 'RUN'; printf ' <%s>' "$@"; printf '\n'; }
confirm(){ return 0; }
ask(){
  printf 'ASK <%s>\n' "$1" >&3
  case "$1" in
    *服务器地址*) printf '%s\n' "$TCPFIT_TEST_MANUAL" ;;
    *) printf '%s\n' "${2:-}" ;;
  esac
}
ip(){
  printf 'ROUTE <%s>\n' "$*" >&3
  printf '%s\n' "$TCPFIT_TEST_ROUTE"
}
curl(){
  printf 'CURL <%s>\n' "$*" >&3
  case "$*" in
    *api64.ipify.org*) printf '%s\n' "$TCPFIT_TEST_IPIFY"; return "$TCPFIT_TEST_IPIFY_STATUS" ;;
    *icanhazip.com*) printf '%s\n' "$TCPFIT_TEST_FALLBACK"; return "$TCPFIT_TEST_FALLBACK_STATUS" ;;
    *) return 1 ;;
  esac
}
'''


def run_shell(code, *args, env=None):
    return subprocess.run([BASH, "-c", 'source "$1"\nshift\n' + code, "test-entry", (ROOT / "tcpfit.sh").as_posix(), *args],
                          env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)


class ReturnEntryTests(unittest.TestCase):
    def run_return(self, *args, route="1.1.1.1 dev eth0 src 8.8.8.8 uid 0", ipify="", fallback="",
                   manual="", ipify_status=0, fallback_status=0, extra="", bandwidth=1000):
        env = dict(os.environ, TCPFIT_TEST_ROUTE=route, TCPFIT_TEST_IPIFY=ipify, TCPFIT_TEST_FALLBACK=fallback,
                   TCPFIT_TEST_MANUAL=manual, TCPFIT_TEST_IPIFY_STATUS=str(ipify_status),
                   TCPFIT_TEST_FALLBACK_STATUS=str(fallback_status))
        defaults = ("--client-bw", str(bandwidth)) if bandwidth is not None else ()
        return run_shell(RETURN_STUBS + extra + '\ncmd_return "$@"', *defaults, *args, env=env)

    def assert_started(self, result, address, family=4):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("<--server> <{}>".format(address), result.stdout)
        self.assertIn("<--family> <{}>".format(family), result.stdout)

    def test_public_route_source_is_used_without_address_question(self):
        result = self.run_return()
        self.assert_started(result, "8.8.8.8")
        self.assertNotIn("CURL <", result.stderr)
        self.assertNotIn("家宽可访问的服务器地址", result.stderr)

    def test_private_route_uses_public_egress_query(self):
        result = self.run_return(route="1.1.1.1 via 10.0.0.1 dev eth0 src 10.0.0.2", ipify="8.8.4.4\r")
        self.assert_started(result, "8.8.4.4")
        self.assertIn("CURL <-4", result.stderr)
        self.assertIn("--noproxy *", result.stderr)
        self.assertNotIn("家宽可访问的服务器地址", result.stderr)

    def test_query_failure_or_invalid_reply_tries_next_service(self):
        for value, status in (("", 28), ("<html>unavailable</html>", 0), ("fd00::1", 0)):
            with self.subTest(value=value, status=status):
                result = self.run_return("--yes", route="", ipify=value, ipify_status=status, fallback="1.1.1.1")
                self.assert_started(result, "1.1.1.1")
                self.assertIn("icanhazip.com", result.stderr)
                self.assertNotIn("ASK <", result.stderr)

    def test_failed_detection_prompts_once_and_uses_manual_address(self):
        result = self.run_return(route="", ipify_status=28, fallback_status=28, manual="server.example")
        self.assert_started(result, "server.example")
        self.assertIn("未能自动获取", result.stderr)
        self.assertEqual(result.stderr.count("ASK <  家宽可访问的服务器地址"), 1)

    def test_empty_manual_address_stops_before_other_questions_or_dependencies(self):
        result = self.run_return(route="")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr.count("ASK <"), 1)
        self.assertNotIn("DEPENDENCIES", result.stdout)
        self.assertNotIn("RUN", result.stdout)

    def test_yes_detection_failure_stops_without_questions(self):
        result = self.run_return("--yes", route="", ipify_status=28, fallback_status=28)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--server", result.stderr)
        self.assertNotIn("ASK <", result.stderr)
        self.assertNotIn("DEPENDENCIES", result.stdout)

    def test_explicit_address_bypasses_detection(self):
        for args in ((), ("--yes",)):
            with self.subTest(args=args):
                result = self.run_return("--server", "server.example", *args)
                self.assert_started(result, "server.example")
                self.assertNotIn("ROUTE <", result.stderr)
                self.assertNotIn("CURL <", result.stderr)
                self.assertNotIn("家宽可访问的服务器地址", result.stderr)

    def test_ports_default_to_fixed_values_in_interactive_and_yes_modes(self):
        for args in ((), ("--yes",)):
            with self.subTest(args=args):
                result = self.run_return(*args)
                self.assert_started(result, "8.8.8.8")
                self.assertIn("<--control-port> <12223>", result.stdout)
                self.assertIn("<--iperf-port> <12224>", result.stdout)
                self.assertIn("12223 / 12224 TCP", result.stdout)
                self.assertNotIn("自动选择", result.stdout)
                self.assertNotIn("端口 TCP", result.stderr)

    def test_existing_port_options_remain_compatible_without_questions(self):
        fixed = self.run_return("--control-port", "45211", "--iperf-port", "45212")
        self.assert_started(fixed, "8.8.8.8")
        self.assertIn("<--control-port> <45211>", fixed.stdout)
        self.assertIn("<--iperf-port> <45212>", fixed.stdout)
        self.assertNotIn("端口 TCP", fixed.stderr)
        for option, value, other, default in (("--control-port", "45211", "--iperf-port", "12224"),
                                              ("--iperf-port", "45212", "--control-port", "12223")):
            with self.subTest(option=option):
                mixed = self.run_return(option, value)
                self.assert_started(mixed, "8.8.8.8")
                self.assertIn("<{}> <{}>".format(option, value), mixed.stdout)
                self.assertIn("<{}> <{}>".format(other, default), mixed.stdout)
                self.assertNotIn("端口 TCP", mixed.stderr)

    def test_invalid_or_duplicate_ports_stop_before_dependencies(self):
        for option in ("--control-port", "--iperf-port"):
            for value in ("-1", "1023", "65536", "1.5", "bad", ""):
                with self.subTest(option=option, value=value):
                    failed = self.run_return("--yes", option, value)
                    self.assertNotEqual(failed.returncode, 0)
                    self.assertIn("端口必须", failed.stderr)
                    self.assertNotIn("DEPENDENCIES", failed.stdout)
        duplicate = self.run_return("--yes", "--control-port", "45211", "--iperf-port", "45211")
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertIn("不能相同", duplicate.stderr)
        self.assertNotIn("DEPENDENCIES", duplicate.stdout)

    def test_missing_nominal_bandwidth_enables_four_connection_probe(self):
        for args in ((), ("--yes",)):
            with self.subTest(args=args):
                result = self.run_return(*args, bandwidth=None)
                self.assert_started(result, "8.8.8.8")
                self.assertIn("两端均留空，自动用四连接探测", result.stdout)
                self.assertNotIn("<--server-bw>", result.stdout)
                self.assertNotIn("<--client-bw>", result.stdout)

    def test_either_nominal_bandwidth_is_sufficient(self):
        for option in ("--server-bw", "--client-bw"):
            with self.subTest(option=option):
                result = self.run_return("--yes", option, "1000", bandwidth=None)
                self.assert_started(result, "8.8.8.8")
                self.assertIn("<{}> <1000>".format(option), result.stdout)
                self.assertIn("不额外探测", result.stdout)

    def test_repeat_count_defaults_cli_and_interactive_validation(self):
        default = self.run_return("--yes")
        self.assert_started(default, "8.8.8.8")
        self.assertIn("<--repeats> <2>", default.stdout)
        self.assertIn("<--yes>", default.stdout)
        self.assertIn("单连接，每组 2 次", default.stdout)
        self.assertIn("1.5 × BDP 起步", default.stdout)
        self.assertIn("最高 2.5 × BDP", default.stdout)
        self.assertIn("初值不稳定继续试调", default.stdout)
        self.assertIn("不限轮数和总时长", default.stdout)
        self.assertNotIn("最多 8 轮", default.stdout)
        self.assertNotIn("连续 3 轮无收益停止", default.stdout)
        self.assertNotIn("独立复测", default.stdout)
        self.assertNotIn("分两阶段", default.stdout)
        custom = self.run_return("--yes", "--repeats", "5")
        self.assert_started(custom, "8.8.8.8")
        self.assertIn("<--repeats> <5>", custom.stdout)
        interactive = self.run_return(extra='ask(){ case "$1" in *测速次数*) echo 4 ;; *) echo "${2:-}" ;; esac; }')
        self.assert_started(interactive, "8.8.8.8")
        self.assertIn("<--repeats> <4>", interactive.stdout)
        for value in ("0", "1", "11", "-1", "2.5", "bad", ""):
            with self.subTest(value=value):
                failed = self.run_return("--yes", "--repeats", value)
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn("测速次数必须", failed.stderr)
                self.assertNotIn("DEPENDENCIES", failed.stdout)
        failed = self.run_return(extra='ask(){ case "$1" in *测速次数*) echo bad ;; *) echo "${2:-}" ;; esac; }')
        self.assertNotEqual(failed.returncode, 0)
        self.assertNotIn("DEPENDENCIES", failed.stdout)

    def test_ipv6_uses_selected_family_for_route_and_public_query(self):
        for route in ("2606:4700:4700::1111 dev eth0 src 2001:4860:4860::8888", ""):
            with self.subTest(route=route):
                result = self.run_return("-6", "--yes", route=route, ipify="2001:4860:4860::8888")
                self.assert_started(result, "2001:4860:4860::8888", 6)
                self.assertIn("ROUTE <-6 route get", result.stderr)
                if not route:
                    self.assertIn("CURL <-6", result.stderr)

    def test_ipv6_only_host_is_automatic_unless_ipv4_was_explicit(self):
        extra = "have_ipv4(){ return 1; }\nhave_ipv6(){ return 0; }\n"
        route = "2606:4700:4700::1111 dev eth0 src 2001:4860:4860::8888"
        result = self.run_return("--yes", route=route, extra=extra)
        self.assert_started(result, "2001:4860:4860::8888", 6)
        result = self.run_return("-4", "--yes", route=route, extra=extra)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ROUTE <-4 route get", result.stderr)
        self.assertNotIn("RUN", result.stdout)

    def test_unusable_or_wrong_family_query_addresses_are_rejected(self):
        addresses = {
            "-4": ("10.0.0.2", "100.64.0.1", "127.0.0.1", "169.254.1.1", "172.16.0.1", "192.168.1.1",
                   "203.0.113.1", "224.0.0.1", "999.1.1.1", "01.1.1.1", "1.1.1.1\n8.8.8.8", "2001:4860::1"),
            "-6": ("::1", "fe80::1", "fd00::1", "::ffff:8.8.8.8", "2001:db8::1", "2001:4860::1::2",
                   "2001:4860:::1", "2001:4860:1:2:3:4:5:6:7", "2001:4860::gg", "8.8.8.8"),
        }
        for family, values in addresses.items():
            for value in values:
                with self.subTest(family=family, address=value):
                    result = self.run_return(family, "--yes", route="", ipify=value, fallback=value)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertNotIn("DEPENDENCIES", result.stdout)
                    self.assertNotIn("RUN", result.stdout)

    def test_wget_can_detect_when_curl_is_missing(self):
        extra = r'''
command(){
  if [ "${1:-}" = -v ] && [ "${2:-}" = curl ]; then return 1; fi
  builtin command "$@"
}
wget(){ printf 'WGET <%s>\n' "$*" >&3; printf '8.8.4.4\n'; }
'''
        result = self.run_return("--yes", route="", extra=extra)
        self.assert_started(result, "8.8.4.4")
        self.assertIn("WGET <-4", result.stderr)
        self.assertIn("--no-proxy", result.stderr)


class MenuEntryTests(unittest.TestCase):
    def test_displayed_menu_numbers_are_contiguous(self):
        result = run_shell(r'''
detect_iface(){ printf 'eth0\n'; }
detect_ram_mb(){ printf '1024\n'; }
detect_cores(){ printf '1\n'; }
sysctl(){ printf 'bbr\n'; }
tc(){ :; }
clear(){ :; }
telemetry_line(){ printf '0 / 0\n'; }
banner
''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        numbers = re.findall(r"^│\s+(\d+)\.", result.stdout, re.MULTILINE)
        self.assertEqual(numbers, [str(number) for number in range(12)])
        self.assertNotIn("up to 30 min", result.stdout)

    def test_each_numeric_choice_runs_its_intended_action(self):
        actions = [None, "wizard", "cmd_return", "cmd_tune", "cmd_sweep", "cmd_harden", "cmd_status",
                   "cmd_verify", "cmd_rollback", "cmd_update", "archive_list", "cmd_uninstall"]
        stubs = r'''
need_root(){ :; }
take_lock(){ :; }
self_install(){ :; }
telemetry_ping(){ :; }
banner(){ :; }
drain_tty(){ :; }
confirm(){ return 0; }
swapon(){ :; }
auto_pick_peer(){ printf 'peer.example:5201\n'; }
ask(){
  case "$1" in
    *Select*) printf '%s\n' "$TCPFIT_TEST_CHOICE" ;;
    *) printf '%s\n' "${2:-}" ;;
  esac
}
'''
        for action in actions[1:]:
            ending = "return 2" if action == "cmd_return" else "exit 0"
            stubs += "\n" + action + "(){ printf 'ACTION " + action + "\\n'; " + ending + "; }\n"
        for choice, action in enumerate(actions):
            with self.subTest(choice=choice, action=action):
                result = run_shell(stubs + "\nmenu_loop", env=dict(os.environ, TCPFIT_TEST_CHOICE=str(choice)))
                self.assertEqual(result.returncode, 2 if choice == 2 else 0, result.stdout + result.stderr)
                actual = [line for line in result.stdout.splitlines() if line.startswith("ACTION ")]
                self.assertEqual(actual, [] if action is None else ["ACTION " + action])


if __name__ == "__main__":
    unittest.main()
