"""手动缓冲区命令回归；以临时文件模拟内核参数，保留真实配置写入和恢复过程。"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") or "bash"
MIB = 1048576
INITIAL = {
    "net.core.rmem_max": str(16 * MIB),
    "net.core.wmem_max": str(24 * MIB),
    "net.core.rmem_default": str(2 * MIB),
    "net.core.wmem_default": str(3 * MIB),
    "net.ipv4.tcp_rmem": "4096 {} {}".format(4 * MIB, 16 * MIB),
    "net.ipv4.tcp_wmem": "4096 {} {}".format(6 * MIB, 24 * MIB),
}
STUBS = r'''
STATE_DIR="$TCPFIT_TEST_BUFFER_DIR/state"
SYSCTL_FILE="$TCPFIT_TEST_BUFFER_DIR/sysctl.conf"
need_root(){ printf 'ROOT\n' >> "$TCPFIT_TEST_BUFFER_DIR/actions"; }
take_lock(){ printf 'LOCK\n' >> "$TCPFIT_TEST_BUFFER_DIR/actions"; }
take_snapshot(){ printf 'SNAPSHOT\n' >> "$TCPFIT_TEST_BUFFER_DIR/actions"; }
archive_save(){ printf 'ARCHIVE %s\n' "$1" >> "$TCPFIT_TEST_BUFFER_DIR/actions"; }
sysctl(){
  local key value
  case "$1" in
    -n) cat "$TCPFIT_TEST_BUFFER_DIR/kernel.$2" ;;
    -qw)
      key=${2%%=*}; value=${2#*=}
      printf '%s\n' "$2" >> "$TCPFIT_TEST_BUFFER_DIR/writes"
      if [ "$key" = net.core.wmem_max ] && [ ! -e "$TCPFIT_TEST_BUFFER_DIR/fault-used" ]; then
        case "${TCPFIT_TEST_BUFFER_FAULT:-}" in
          reject) : > "$TCPFIT_TEST_BUFFER_DIR/fault-used"; return 1 ;;
          clamp) : > "$TCPFIT_TEST_BUFFER_DIR/fault-used"; value=123456 ;;
          signal)
            : > "$TCPFIT_TEST_BUFFER_DIR/fault-used"
            printf '%s\n' "$value" > "$TCPFIT_TEST_BUFFER_DIR/kernel.$key"
            kill -TERM "$BASHPID"
            return 1 ;;
        esac
      fi
      printf '%s\n' "$value" > "$TCPFIT_TEST_BUFFER_DIR/kernel.$key" ;;
    *) return 99 ;;
  esac
}
mv(){
  if [ "${TCPFIT_TEST_BUFFER_FAULT:-}" = save ] &&
     [[ "${@: -2:1}" == *.buffer.* ]] && [ "${@: -1}" = "$SYSCTL_FILE" ]; then return 1; fi
  command mv "$@"
}
'''


class BufferCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.config = self.directory / "sysctl.conf"
        for key, value in INITIAL.items():
            (self.directory / ("kernel." + key)).write_text(value + "\n", encoding="utf-8")
        self.original = ("# 原配置说明\nnet.ipv4.tcp_congestion_control = cubic\nvm.swappiness = 12\n"
                         "# 缓冲区：上限=旧推导值\n" +
                         "".join("{} = {}\n".format(key, value) for key, value in INITIAL.items()) +
                         "-net/core/rmem_max = 999\n")
        self.config.write_text(self.original, encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def run_command(self, *args, fault="", extra=""):
        env = dict(os.environ, TCPFIT_TEST_BUFFER_DIR=self.directory.as_posix(), TCPFIT_TEST_BUFFER_FAULT=fault,
                   TCPFIT_NO_TELEMETRY="1")
        return subprocess.run(
            [BASH, "-c", 'source "$1"\nshift\n' + STUBS + extra + '\ncmd_buffer "$@"',
             "test-buffer", (ROOT / "tcpfit.sh").as_posix(), *args],
            env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)

    def kernel(self):
        return {key: (self.directory / ("kernel." + key)).read_text(encoding="utf-8").strip() for key in INITIAL}

    def assert_persistent_matches_kernel(self):
        content = self.config.read_text(encoding="utf-8")
        for key, value in self.kernel().items():
            self.assertEqual(content.count(key + " = "), 1, content)
            self.assertIn(key + " = " + value + "\n", content)
        self.assertIn("# 原配置说明\n", content)
        self.assertIn("net.ipv4.tcp_congestion_control = cubic\n", content)
        self.assertIn("vm.swappiness = 12\n", content)
        self.assertNotIn("net/core/rmem_max", content)
        self.assertNotIn("旧推导值", content)

    def test_setting_maximum_preserves_individual_defaults_and_other_parameters(self):
        result = self.run_command("--max-mb", "32")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        actual = self.kernel()
        self.assertEqual(actual["net.core.rmem_max"], str(32 * MIB))
        self.assertEqual(actual["net.core.wmem_max"], str(32 * MIB))
        self.assertEqual(actual["net.ipv4.tcp_rmem"], "4096 {} {}".format(4 * MIB, 32 * MIB))
        self.assertEqual(actual["net.ipv4.tcp_wmem"], "4096 {} {}".format(6 * MIB, 32 * MIB))
        self.assertEqual(actual["net.core.wmem_default"], str(3 * MIB))
        self.assert_persistent_matches_kernel()
        self.assertIn("16 MiB → 32 MiB", result.stdout)
        self.assertIn("24 MiB → 32 MiB", result.stdout)
        actions = (self.directory / "actions").read_text(encoding="utf-8").splitlines()
        self.assertEqual(actions, ["ROOT", "LOCK", "SNAPSHOT", "ARCHIVE before-buffer-32MiB", "ARCHIVE buffer-32MiB"])
        self.assertFalse(list(self.directory.glob("sysctl.conf.buffer*")))

    def test_lower_maximum_clamps_all_defaults_and_preserves_tcp_minimum(self):
        result = self.run_command("--max-mb", "2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for key, value in self.kernel().items():
            self.assertEqual(value, "4096 {0} {0}".format(2 * MIB) if "tcp_" in key else str(2 * MIB))
        self.assert_persistent_matches_kernel()

    def test_explicit_default_and_leading_zero_size_are_decimal(self):
        result = self.run_command("--max-mb", "008", "--default-mb", "01")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        actual = self.kernel()
        self.assertEqual(actual["net.ipv4.tcp_wmem"], "4096 {} {}".format(MIB, 8 * MIB))
        self.assertEqual(actual["net.core.rmem_default"], str(MIB))
        self.assert_persistent_matches_kernel()

    def test_byte_sized_candidate_preserves_small_tcp_minimum_and_default(self):
        for key in INITIAL:
            value = "1875 1875 1875" if "tcp_" in key else "1875"
            (self.directory / ("kernel." + key)).write_text(value + "\n", encoding="utf-8")
        result = self.run_command("3125", "1875", extra='cmd_buffer(){ apply_buffer_config "$@"; }')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        actual = self.kernel()
        for key in ("net.ipv4.tcp_rmem", "net.ipv4.tcp_wmem"):
            self.assertEqual(actual[key], "1875 1875 3125")
        self.assertEqual(actual["net.core.rmem_max"], "3125")
        self.assertEqual(actual["net.core.rmem_default"], "1875")
        self.assert_persistent_matches_kernel()

    def test_bdp_candidate_above_one_gib_is_applied_but_integer_overflow_is_rejected(self):
        extra = 'cmd_buffer(){ apply_buffer_config "$@"; }'
        result = self.run_command("1250000000", str(MIB), extra=extra)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        actual = self.kernel()
        self.assertEqual(actual["net.ipv4.tcp_rmem"], "4096 {} 1250000000".format(MIB))
        self.assert_persistent_matches_kernel()
        writes = (self.directory / "writes").read_text(encoding="utf-8")
        result = self.run_command("2147483648", str(MIB), extra=extra)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.kernel(), actual)
        self.assertEqual((self.directory / "writes").read_text(encoding="utf-8"), writes)

    def test_invalid_arguments_are_rejected_before_lock_snapshot_or_writes(self):
        inputs = [(), ("--max-mb",), ("--max-mb", "0"), ("--max-mb", "-1"), ("--max-mb", "1.5"),
                  ("--max-mb", "1025"), ("--max-mb", "9999999999999999999999999999"),
                  ("--max-mb", "32", "--default-mb", "33"), ("--max-mb", "32", "--default-mb", ""),
                  ("--max-mb", "32", "--default-mb", "0"), ("--default-mb", "1"), ("--unknown",)]
        for args in inputs:
            with self.subTest(args=args):
                result = self.run_command(*args)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse((self.directory / "actions").exists())
                self.assertFalse((self.directory / "writes").exists())
                self.assertEqual(self.kernel(), INITIAL)
                self.assertEqual(self.config.read_text(encoding="utf-8"), self.original)

    def test_help_needs_no_root_and_is_available_through_public_entry(self):
        result = subprocess.run([BASH, (ROOT / "tcpfit.sh").as_posix(), "buffer", "--help"],
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("--max-mb", result.stdout)
        self.assertIn("1048576", result.stdout)
        self.assertFalse((self.directory / "actions").exists())

    def test_missing_kernel_parameter_stops_before_snapshot_or_writes(self):
        (self.directory / "kernel.net.ipv4.tcp_wmem").unlink()
        result = self.run_command("--max-mb", "32")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("无法读取", result.stderr)
        self.assertNotIn("SNAPSHOT", (self.directory / "actions").read_text(encoding="utf-8"))
        self.assertFalse((self.directory / "writes").exists())

    def test_new_configuration_can_be_created_without_prior_tuning(self):
        self.config.unlink()
        result = self.run_command("--max-mb", "32")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("net.core.rmem_max = " + str(32 * MIB), self.config.read_text(encoding="utf-8"))

    def test_rejected_clamped_interrupted_and_unsaved_changes_restore_every_value(self):
        for fault in ("reject", "clamp", "signal", "save"):
            with self.subTest(fault=fault):
                (self.directory / "fault-used").unlink(missing_ok=True)
                result = self.run_command("--max-mb", "32", fault=fault)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.kernel(), INITIAL)
                self.assertEqual(self.config.read_text(encoding="utf-8"), self.original)
                self.assertIn("已恢复修改前的值", result.stderr)
                self.assertNotIn("已设为", result.stdout)
                self.assertFalse(list(self.directory.glob("sysctl.conf.buffer*")))

    def test_conflicting_minimum_is_not_silently_overwritten(self):
        value = "{} {} {}".format(3 * MIB, 4 * MIB, 16 * MIB)
        (self.directory / "kernel.net.ipv4.tcp_rmem").write_text(value, encoding="utf-8")
        result = self.run_command("--max-mb", "2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("最小值", result.stderr)
        self.assertFalse((self.directory / "writes").exists())
        self.assertEqual(self.config.read_text(encoding="utf-8"), self.original)


if __name__ == "__main__":
    unittest.main()
