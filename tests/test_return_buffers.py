"""优化线路调优缓冲区试调回归：模拟速度、重传、内存与异常，验证搜索和回滚。"""
import copy
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("tcpfit_return_buffers", ROOT / "tcpfit-return.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
MIB = MODULE.MIB
MEMORY = {"total_bytes": 512 * MIB, "available_bytes": 384 * MIB, "reserve_bytes": 32 * MIB}


def buffer_state(maximum=16, initial=1):
    return {
        "net.core.rmem_max": [maximum * MIB], "net.core.wmem_max": [maximum * MIB],
        "net.core.rmem_default": [initial * MIB], "net.core.wmem_default": [initial * MIB],
        "net.ipv4.tcp_rmem": [4096, initial * MIB, maximum * MIB],
        "net.ipv4.tcp_wmem": [4096, initial * MIB, maximum * MIB],
    }


def rows(single=100, four=200, retrans=0.01, four_retrans=None):
    return [{"streams": streams, "receiver_mbps": speed, "estimated_retrans_pct": ratio, "retransmits": 10,
             "latency": {"idle": {"mean_ms": 100}, "loaded": {"mean_ms": 120}}}
            for streams, speed, ratio in ((1, single, retrans), (4, four, retrans if four_retrans is None else four_retrans))
            for _ in range(2)]


class BufferWorker:
    def __init__(self, state):
        self.state = copy.deepcopy(state)
        self.rules = MODULE.Worker(ROOT / "tcpfit.sh")
        self.changes = []

    def run(self, action, *args, **kwargs):
        if action == "buffers":
            return "\n".join(key + "=" + " ".join(map(str, values)) for key, values in self.state.items()), 0
        if action == "buffer":
            maximum = int(args[0])
            self.changes.append(tuple(args))
            for key in ("net.core.rmem_max", "net.core.wmem_max"):
                self.state[key] = [maximum]
            for key in ("net.core.rmem_default", "net.core.wmem_default", "net.ipv4.tcp_rmem", "net.ipv4.tcp_wmem"):
                index = 1 if "tcp_" in key else 0
                self.state[key][index] = int(args[1]) if len(args) > 1 else min(self.state[key][index], maximum)
                if index:
                    self.state[key][2] = maximum
            return "", 0
        return self.rules.run(action, *args, **kwargs)


class BufferTrialTests(unittest.TestCase):
    def search(self, initial_rows, candidates, *, state=None, baseline=None, limit=64, rounds=8, memory=None, can_trial=None):
        worker = BufferWorker(state or buffer_state())
        pending, stages, trials, search = list(candidates), [], [], {}
        output = io.StringIO()

        def measure(stage, modes=(1, 4)):
            stages.append((stage, modes, copy.deepcopy(worker.state)))
            self.assertTrue(pending, "不应增加未经安排的测速轮次")
            expected_mode, group = pending.pop(0)
            self.assertEqual(modes, (expected_mode,), "试调和切换只能测当前连接数")
            return [row for row in group if row["streams"] == expected_mode]

        with tempfile.TemporaryDirectory() as directory, redirect_stdout(output), \
                mock.patch.object(MODULE, "read_buffer_memory", return_value=memory or MEMORY), \
                mock.patch.object(MODULE, "BUFFER_TRIALS", rounds):
            path = Path(directory) / "buffer-trials.json"
            chosen_rows, chosen_state = MODULE.tune_buffers(
                worker, baseline or initial_rows, initial_rows, copy.deepcopy(worker.state), limit * MIB,
                measure, trials, path, search, can_trial)
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved, trials)
        self.assertEqual(worker.state, chosen_state)
        self.assertFalse(pending, "应完成预期的候选验证")
        return worker, chosen_rows, chosen_state, trials, stages, output.getvalue(), search

    def test_good_modes_do_not_increase_buffers_for_speed(self):
        worker, _, state, trials, stages, _, search = self.search(rows(), [(1, rows())])
        self.assertFalse(worker.changes)
        self.assertFalse(trials)
        self.assertEqual(state, buffer_state())
        self.assertEqual(stages[0][1], (1,))
        self.assertEqual(search["order"], [4, 1])
        self.assertTrue(all(phase["goal_reached"] for phase in search["phases"]))

    def test_four_high_retransmission_triggers_adjustment_then_fresh_single_check(self):
        initial = rows(retrans=0.8, four_retrans=6)
        worker, _, state, trials, stages, output, search = self.search(initial, [
            (4, rows(retrans=0.8, four_retrans=0.8)),
            (1, rows(retrans=0.8, four_retrans=0.8)),
            (1, rows(retrans=0.3, four_retrans=0.8)),
        ])
        self.assertEqual(worker.changes, [(14 * MIB, MIB), (13 * MIB, MIB)])
        self.assertEqual([trial["streams"] for trial in trials], [4, 1])
        self.assertEqual([item[1] for item in stages], [(4,), (1,), (1,)])
        self.assertIn("第二阶段切换检查", stages[1][0])
        self.assertEqual(stages[1][2], buffer_state(14))
        self.assertEqual(state, buffer_state(13))
        self.assertIn("本轮暂留", output)
        self.assertIn("6.000%", output)
        self.assertIn("16 MiB → 14 MiB", output)
        self.assertEqual(search["order"], [4, 1])

    def test_severity_is_normalized_and_close_scores_prioritize_four(self):
        self.assertEqual(MODULE.priority_modes(rows(retrans=1.6, four_retrans=6)), (1, 4))
        self.assertEqual(MODULE.priority_modes(rows(retrans=1, four_retrans=5)), (4, 1))
        self.assertEqual(MODULE.priority_modes(rows(retrans=1.04, four_retrans=5)), (4, 1))
        self.assertEqual(MODULE.priority_modes(rows(retrans=0.7, four_retrans=4)), (4, 1))

    def test_switch_does_not_reuse_old_good_four_measurements(self):
        worker, _, state, trials, stages, _, search = self.search(rows(retrans=1.6, four_retrans=0.2), [
            (1, rows(retrans=0.4)),
            (4, rows(retrans=0.4, four_retrans=6)),
            (4, rows(retrans=0.4, four_retrans=0.9)),
        ])
        self.assertEqual([trial["streams"] for trial in trials], [1, 4])
        self.assertEqual(worker.changes, [(14 * MIB, MIB), (12 * MIB, MIB)])
        self.assertEqual(state, buffer_state(12))

    def test_exact_thresholds_are_inclusive_and_high_thresholds_are_strict(self):
        baseline = rows(retrans=2, four_retrans=8)
        self.assertTrue(MODULE.mode_goal(baseline, rows(95, 190, retrans=0.5, four_retrans=1), 1))
        self.assertTrue(MODULE.mode_goal(baseline, rows(95, 190, retrans=0.5, four_retrans=1), 4))
        for ratio, step in ((5, 1), (5.001, 2)):
            worker, _, _, _, _, _, _ = self.search(rows(four_retrans=ratio), [
                (4, rows(four_retrans=0.5)), (1, rows())])
            self.assertEqual(worker.changes[0][0], (16 - step) * MIB)

    def test_eight_round_limit_is_shared_by_both_stages(self):
        initial = rows(four_retrans=40)
        candidates = [(4, rows(four_retrans=40 - step * 4)) for step in range(1, 9)]
        worker, _, _, trials, _, output, search = self.search(initial, candidates, state=buffer_state(32))
        self.assertEqual(len(trials), 8)
        self.assertEqual(search["phases"][1]["status"], "skipped")
        self.assertIn("合计达到最大 8 轮", output)
        self.assertEqual([item[0] for item in worker.changes], [value * MIB for value in range(30, 15, -2)])

    def test_coarse_rejection_rolls_back_then_shrinks_step(self):
        worker, _, state, trials, _, output, _ = self.search(rows(four_retrans=6), [
            (4, rows(four=180, four_retrans=0.5)),
            (4, rows(four_retrans=0.5)),
            (1, rows()),
        ])
        self.assertEqual(worker.changes, [(14 * MIB, MIB), (16 * MIB, MIB), (15 * MIB, MIB)])
        self.assertEqual([trial["kept"] for trial in trials], [False, True])
        self.assertEqual(state, buffer_state(15))
        self.assertIn("回退后缩小步长", output)

    def test_three_no_gain_rounds_stop_after_reverse_fine_tuning(self):
        initial = rows(four_retrans=6)
        worker, _, state, trials, _, output, _ = self.search(initial, [(4, initial)] * 3)
        self.assertEqual([trial["target_max_bytes"] for trial in trials], [14 * MIB, 15 * MIB, 17 * MIB])
        self.assertTrue(all(not trial["kept"] for trial in trials))
        self.assertEqual(state, buffer_state())
        self.assertIn("反向细调", output)
        self.assertIn("连续 3 轮无收益", output)

    def test_small_successive_speed_losses_cannot_accumulate_below_original_baseline(self):
        initial = rows(four_retrans=6)
        worker, _, state, trials, _, output, _ = self.search(initial, [
            (4, rows(four=192, four_retrans=4)),
            (4, rows(four=184.32, four_retrans=2)),
        ], rounds=2)
        self.assertEqual([trial["kept"] for trial in trials], [True, False])
        self.assertEqual(state, buffer_state(14))
        self.assertIn("相对原始基线", output)

    def test_faster_other_mode_cannot_offset_active_mode_speed_regression(self):
        _, _, state, trials, _, _, _ = self.search(rows(four_retrans=6), [
            (4, rows(single=1000, four=189, four_retrans=0.5))], rounds=1)
        self.assertFalse(trials[0]["kept"])
        self.assertEqual(state, buffer_state())

    def test_unstable_results_are_not_improvement(self):
        candidate = rows(four_retrans=0.5)
        candidate[-1]["receiver_mbps"] = 100
        _, _, state, trials, _, output, _ = self.search(rows(four_retrans=6), [(4, candidate)], rounds=1)
        self.assertFalse(trials[0]["kept"])
        self.assertEqual(state, buffer_state())
        self.assertIn("不稳定", output)

    def test_failed_downward_trial_restores_defaults_as_well_as_maxima(self):
        worker, _, state, trials, stages, _, _ = self.search(
            rows(four_retrans=6), [(4, rows(four=180, four_retrans=0.5))], state=buffer_state(8, 8), rounds=1)
        self.assertEqual(stages[0][2], buffer_state(6, 6))
        self.assertEqual(worker.changes, [(6 * MIB, 6 * MIB), (8 * MIB, 8 * MIB)])
        self.assertEqual(state, buffer_state(8, 8))

    def test_time_reserve_stops_trials_before_mutation(self):
        worker, _, _, trials, _, output, _ = self.search(rows(four_retrans=6), [], can_trial=lambda: False)
        self.assertFalse(worker.changes)
        self.assertFalse(trials)
        self.assertIn("剩余时间", output)

    def test_memory_constraint_stops_reverse_growth(self):
        initial = rows(four_retrans=6)
        worker, _, state, trials, _, output, _ = self.search(
            initial, [(4, initial)] * 2, memory=dict(MEMORY, available_bytes=36 * MIB))
        self.assertEqual(len(trials), 2)
        self.assertEqual(state, buffer_state())
        self.assertIn("内存约束", output)

    def test_visited_neighbors_and_bounds_do_not_repeat_candidates(self):
        visited = {16 * MIB, 18 * MIB, 20 * MIB}
        self.assertEqual(MODULE.next_buffer_target(18 * MIB, 24 * MIB, 1, 2 * MIB, visited), (19 * MIB, 1, MIB))
        visited.update({17 * MIB, 19 * MIB})
        self.assertIsNone(MODULE.next_buffer_target(18 * MIB, 24 * MIB, 1, 2 * MIB, visited))
        self.assertEqual(MODULE.next_buffer_target(4 * MIB, 8 * MIB, -1, MIB, {4 * MIB}), (5 * MIB, 1, MIB))

    def test_missing_or_inconsistent_actual_buffer_values_are_rejected(self):
        with self.assertRaisesRegex(MODULE.TaskError, "缓冲区参数"):
            MODULE.buffers_from_sysctl({})
        state = buffer_state(16)
        state["net.core.wmem_max"] = [15 * MIB]
        with self.assertRaisesRegex(MODULE.TaskError, "实际上限"):
            MODULE.check_buffer_target(state, 16 * MIB)

    def test_measurement_failure_interrupt_and_memory_pressure_restore_previous_buffers(self):
        for failure in (MODULE.TaskError("测速失败"), KeyboardInterrupt(), "memory"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
                worker = BufferWorker(buffer_state())
                trials = []
                measure = mock.Mock(return_value=rows(four_retrans=0.5))
                memories = [MEMORY, dict(MEMORY, available_bytes=16 * MIB)] if failure == "memory" else [MEMORY]
                if failure != "memory":
                    measure.side_effect = failure
                with mock.patch.object(MODULE, "read_buffer_memory", side_effect=memories), \
                        self.assertRaises(MODULE.TaskError if failure == "memory" else type(failure)):
                    MODULE.tune_buffers(worker, rows(four_retrans=6), rows(four_retrans=6),
                                        buffer_state(), 24 * MIB, measure, trials, Path(directory) / "trials.json")
                self.assertEqual(worker.state, buffer_state())
                self.assertEqual(trials[0]["status"], "failed")
                self.assertEqual(trials[0]["restored"], buffer_state())


class BufferProfileTests(unittest.TestCase):
    def shell(self, expression):
        result = subprocess.run(
            [shutil.which("bash") or "bash", "-c", 'source "$1"\n' + expression,
             "test-buffer-profile", (ROOT / "tcpfit.sh").as_posix()],
            capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_memory_tiers_are_monotonic_and_512_mib_allows_24_mib(self):
        memories = [128, 256, 447, 448, 480, 512, 640, 768, 1024, 8192, 16384]
        actual = list(map(int, self.shell('for ram in ' + ' '.join(map(str, memories)) + '; do calc_buf_limit "$ram"; echo; done').split()))
        expected = [4 * MIB, 8 * MIB, 447 * 32768, 24 * MIB, 24 * MIB, 24 * MIB,
                    24 * MIB, 24 * MIB, 32 * MIB, 256 * MIB, 256 * MIB]
        self.assertEqual(actual, expected)
        self.assertEqual(actual, sorted(actual))

    def test_bdp_initial_value_is_not_forced_to_the_24_mib_ceiling(self):
        profile = list(map(int, self.shell("calc_buffer_profile proxy 200 100 512").split()))
        self.assertEqual(profile, [2500000, 3750000 + 2 * MIB, MIB, 24 * MIB])

    def test_return_worker_uses_two_bdp_without_changing_regular_profile(self):
        for ram, bandwidth, rtt, expected_max in ((2048, 1000, 180, 45000000),
                                                  (512, 1000, 180, 24 * MIB),
                                                  (512, 200, 100, 5000000),
                                                  (128, 10, 20, 4 * MIB)):
            with self.subTest(ram=ram, bandwidth=bandwidth):
                script = MODULE.WORKER.replace('LOCK_HELD=1', 'LOCK_HELD=1\ndetect_ram_mb(){ echo ' + str(ram) + '; }')
                result = subprocess.run([shutil.which("bash") or "bash", "-c", script, "return-profile",
                                         (ROOT / "tcpfit.sh").as_posix(), "buffer-plan", "proxy", str(bandwidth), str(rtt)],
                                        capture_output=True, text=True, encoding="utf-8", timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                profile = list(map(int, result.stdout.split()))
                self.assertEqual(profile[0], bandwidth * rtt * 125)
                self.assertEqual(profile[1], expected_max)

    def test_gigabit_180_ms_profile_uses_one_and_a_half_bdp_with_fixed_headroom(self):
        profile = list(map(int, self.shell("calc_buffer_profile proxy 1000 180 2048").split()))
        self.assertEqual(profile, [22500000, 35847152, MIB, 64 * MIB])
        low_memory = list(map(int, self.shell("calc_buffer_profile proxy 1000 180 512").split()))
        self.assertEqual(low_memory, [22500000, 24 * MIB, MIB, 24 * MIB])

    def test_fractional_byte_is_truncated_and_reason_matches_actual_formula(self):
        maximum = int(self.shell("calc_buf_max 2000001 2048"))
        self.assertEqual(maximum, 3000001 + 2 * MIB)
        self.assertIn("1.5 × BDP + 2 MiB", self.shell("buf_max_reason 2000001 2048 " + str(maximum)))
        self.assertIn("下限 4 MiB", self.shell("buf_max_reason 1250000 512 4194304"))
        self.assertIn("内存试调上限", self.shell("buf_max_reason 22500000 512 25165824"))

    def test_low_memory_defaults_and_global_tcp_budget_stay_bounded(self):
        self.assertEqual(list(map(int, self.shell("calc_buffer_profile bulk 10000 200 128").split())),
                         [250000000, 4 * MIB, 4 * MIB, 4 * MIB])
        self.assertEqual(list(map(int, self.shell("calc_buffer_profile bulk 10000 200 512").split())),
                         [250000000, 24 * MIB, 8 * MIB, 24 * MIB])
        self.assertEqual(list(map(int, self.shell("calc_tcp_mem 512").split())), [8192, 16384, 32768])

    def test_memory_reader_rejects_missing_invalid_or_unreadable_values(self):
        for content in ("MemTotal: 524288 kB\n", "MemTotal: 0 kB\nMemAvailable: 0 kB\n",
                        "MemTotal: 1024 kB\nMemAvailable: 2048 kB\n", "MemTotal: bad kB\nMemAvailable: 4 kB\n"):
            with self.subTest(content=content), mock.patch.object(Path, "read_text", return_value=content):
                with self.assertRaises(MODULE.TaskError):
                    MODULE.read_buffer_memory()
        with mock.patch.object(Path, "read_text", side_effect=OSError("无法读取")):
            with self.assertRaises(MODULE.TaskError):
                MODULE.read_buffer_memory()
        with mock.patch.object(Path, "read_text", return_value="MemTotal: 524288 kB\nMemAvailable: 393216 kB\n"):
            self.assertEqual(MODULE.read_buffer_memory(), MEMORY)


class SnapshotBufferTests(unittest.TestCase):
    def test_shared_recovery_checks_kernel_values_and_still_restores_remaining_items(self):
        values = {key: " ".join(map(str, value)) for key, value in buffer_state(8).items()}
        for mismatch in (False, True):
            with self.subTest(mismatch=mismatch):
                writes, reads = {}, []

                def run(args, **kwargs):
                    output = ""
                    if args[:2] == ["sysctl", "-qw"]:
                        key, value = args[2].split("=", 1)
                        writes[key] = value
                    if args[:2] == ["sysctl", "-n"]:
                        key = args[2]
                        reads.append(key)
                        output = "1" if mismatch and key == "net.core.wmem_max" else values[key].replace(" ", "\t")
                    return subprocess.CompletedProcess(args, 0, output, "")

                state = {"sysctl": values, "files": {}, "routes": {}, "queue": {}, "service_active": False}
                with mock.patch.object(MODULE, "command", side_effect=run), \
                        mock.patch.object(MODULE.QueueState, "restore") as restore_queue:
                    if mismatch:
                        with self.assertRaisesRegex(MODULE.TaskError, "缓冲区实际值不一致"):
                            MODULE.Snapshot.restore(state)
                    else:
                        MODULE.Snapshot.restore(state)
                self.assertEqual(writes, values)
                self.assertEqual(reads, list(MODULE.BUFFER_KEYS))
                restore_queue.assert_called_once_with({})


class ReturnBufferFlowTests(unittest.TestCase):
    def exercise_flow(self, candidate_passes_baseline, failure=None, failure_action="sample",
                      recovery_failure=False, recovery_mismatch=False, selection=None):
        paired = threading.Event()
        paired.set()
        coordinator = types.SimpleNamespace(peer="192.0.2.1", token="test-pairing-placeholder", paired=paired,
                                            done_ack=paired, results=[], started=time.monotonic(), check=mock.Mock(), close=mock.Mock(), fail=mock.Mock())

        class FlowWorker(BufferWorker):
            def run(self, action, *args, **kwargs):
                if action == "profile":
                    return "eth0\n100\n10\n90\n75", 0
                if action == "keys":
                    return "\n".join(MODULE.BUFFER_KEYS), 0
                if action == "probe":
                    return "200", 0
                if action == "buffer-plan":
                    return "{} {} {} {}".format(8 * MIB, 16 * MIB, MIB, 20 * MIB), 0
                if action == "tune":
                    self.state = buffer_state(16)
                    self.congestion = "bbr"
                    return "", 0
                if action == "buffer" and failure is not None and failure_action == "buffer" and int(args[0]) == 14 * MIB:
                    super().run(action, *args, **kwargs)
                    raise failure
                if action == "sample":
                    maximum = self.state["net.ipv4.tcp_rmem"][2] // MIB
                    if failure is not None and maximum == 14:
                        raise failure
                    speed = {8: (100, 200), 16: (100, 210), 14: (99, 205), 13: (97, 200 if candidate_passes_baseline else 150)}
                    ratios = {8: (0.8, 6), 16: (0.8, 6), 14: (0.8, 0.8), 13: (0.3, 0.8)}
                    group = rows(*speed[maximum], retrans=ratios[maximum][0], four_retrans=ratios[maximum][1])
                    sample = next(row for row in group if row["streams"] == args[0])
                    coordinator.results.append(sample)
                    return "", 0
                if action == "band":
                    return "0", 0
                if action == "spike":
                    return "", 1
                if action in ("snapshot", "archive", "fq"):
                    return "", 0
                return super().run(action, *args, **kwargs)

        worker = FlowWorker(buffer_state(8))
        worker.congestion = "cubic"

        def snapshot(*args):
            return {"sysctl": dict({key: " ".join(map(str, values)) for key, values in worker.state.items()},
                                   **{"net.ipv4.tcp_congestion_control": worker.congestion}),
                    "queue": {"rate": None, "limited_fq": False}, "service_active": False,
                    "files": {}, "routes": {}}

        def restore(saved):
            if recovery_failure:
                raise MODULE.TaskError("模拟完整快照暂时无法恢复")
            worker.state = MODULE.buffers_from_sysctl(saved["sysctl"])
            worker.congestion = saved["sysctl"]["net.ipv4.tcp_congestion_control"]
            if recovery_mismatch:
                worker.state["net.core.wmem_max"] = [9 * MIB]

        with tempfile.TemporaryDirectory() as directory:
            task_dir = Path(directory)
            args = types.SimpleNamespace(script=str(ROOT / "tcpfit.sh"), client_script=str(ROOT / "tcpfit-client.sh"),
                                         state_dir=str(task_dir / "state"), lock_fd=9, role="proxy", family=4,
                                         server="192.0.2.2", server_bw=None, client_bw=None, token_ttl=600,
                                         control_port=5211, iperf_port=5212, repeats=2, yes=selection is None)
            coordinator.run_dir = task_dir / "runtime"
            firewall = mock.Mock(state={"backend": "none", "manager": None}, path=task_dir / "firewall.json")
            guardian = mock.Mock()
            output = io.StringIO()

            def private_path(value):
                return task_dir / "runtime" if str(value) == "/run/tcpfit" else Path(value)

            selector = MODULE.select_configuration

            def choose(book, recommendation, automatic, **kwargs):
                return selector(book, recommendation, automatic, reader=lambda: str(selection))

            with mock.patch.object(MODULE, "validate_environment"), \
                    mock.patch.object(MODULE, "read_buffer_memory", return_value=MEMORY), \
                    mock.patch.object(MODULE, "Path", side_effect=private_path), \
                    mock.patch.object(MODULE, "process_stamp", return_value="test-process"), \
                    mock.patch.object(MODULE.subprocess, "Popen", return_value=guardian), \
                    mock.patch.object(MODULE, "Coordinator", return_value=coordinator), \
                    mock.patch.object(MODULE, "Worker", return_value=worker), \
                    mock.patch.object(MODULE, "Firewall", return_value=firewall), \
                    mock.patch.object(MODULE, "start_http"), \
                    mock.patch.object(MODULE.signal, "SIGHUP", 1, create=True), \
                    mock.patch.object(MODULE.signal, "signal", return_value=0), \
                    mock.patch.object(MODULE, "command", return_value=subprocess.CompletedProcess([], 0, "192.0.2.1 dev eth0", "")), \
                    mock.patch.object(MODULE.QueueState, "capture"), \
                    mock.patch.object(MODULE.QueueState, "restore"), \
                    mock.patch.object(MODULE.Snapshot, "capture", side_effect=snapshot), \
                    mock.patch.object(MODULE.Snapshot, "restore", side_effect=restore) as restore_call, \
                    mock.patch.object(MODULE, "select_configuration", side_effect=choose), \
                    mock.patch.object(MODULE, "persist_selected_configuration") as persist_call, \
                    redirect_stdout(output):
                code = MODULE.run_locked_task(args)
            self.assertEqual(code, 0 if failure is None else 2, output.getvalue())
            record = next((task_dir / "state" / "return").glob("*/result.json"))
            result = json.loads(record.read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "completed" if failure is None else "failed")
            incomplete = recovery_failure or recovery_mismatch
            self.assertEqual(bool(result.get("cleanup_complete")), not incomplete)
            self.assertEqual((task_dir / "state" / "return-pending.json").exists(), incomplete)
            self.assertEqual(restore_call.call_count, (0 if candidate_passes_baseline and failure is None else 1)
                             + (1 if selection is not None and result.get("selected_config") != result.get("recommended_config") else 0))
            if incomplete:
                self.assertIn("recovery_error", result)
            elif failure is not None:
                self.assertEqual(worker.state, buffer_state(8))
                self.assertEqual(result["buffers_final"], buffer_state(8))
                self.assertFalse(result["base_kept"])
                final = json.loads(record.with_name("final.json").read_text(encoding="utf-8"))
                self.assertEqual(MODULE.buffers_from_sysctl(final["sysctl"]), buffer_state(8))
            else:
                self.assertEqual(result["buffers_final"], worker.state)
                if selection is not None:
                    saved = json.loads(record.with_name("final.json").read_text(encoding="utf-8"))
                    self.assertEqual(saved["sysctl"]["net.ipv4.tcp_congestion_control"], "cubic" if selection <= 6 else "bbr")
                    persist_call.assert_called_once()
            return result, output.getvalue()

    def test_complete_flow_reports_independent_final_measurements(self):
        result, output = self.exercise_flow(True)
        self.assertTrue(result["base_kept"])
        self.assertEqual(result["buffers_final"], buffer_state(13))
        self.assertEqual(result["final"], result["after"])
        self.assertNotEqual(result["after"], result["initial_after"])
        trial_numbers = {row["number"] for trial in result["buffer_trials"] for row in trial["measurements"]}
        self.assertFalse(trial_numbers.intersection(row["number"] for row in result["final"]))
        self.assertEqual(len(result["final"]), 4)
        self.assertIn("最终缓冲区：接收 13 MiB / 发送 13 MiB", output)
        self.assertEqual(result["buffer_search"]["max_rounds"], 8)
        self.assertIn("试调停止原因", output)

    def test_final_validation_rejects_regression_of_the_previously_completed_mode(self):
        result, output = self.exercise_flow(False)
        self.assertFalse(result["base_kept"])
        self.assertTrue(result["buffer_trials"][0]["kept"])
        self.assertEqual(result["buffers_final"], buffer_state(8))
        self.assertNotEqual(result["final"], result["after"])
        self.assertEqual([row["receiver_mbps"] for row in result["final"]], [100, 100, 200, 200])
        self.assertIn("已恢复调优前缓冲区：接收 8 MiB / 发送 8 MiB", output)
        self.assertIn("最终缓冲区：接收 8 MiB / 发送 8 MiB", output)

    def test_manual_sequence_restores_full_configuration_instead_of_recommended_buffers(self):
        result, output = self.exercise_flow(True, selection=3)
        self.assertEqual(result["selected_measurement"], 3)
        self.assertNotEqual(result["selected_config"], result["recommended_config"])
        self.assertEqual(result["buffers_final"], buffer_state(8))
        self.assertTrue(all(row["config_id"] == result["selected_config"] for row in result["final"]))
        self.assertIn("手动选择，未追加验收", result["selected_validation"])

    def test_manual_selection_can_save_a_candidate_rejected_by_final_validation(self):
        result, output = self.exercise_flow(False, selection=17)
        self.assertEqual(result["selected_measurement"], 17)
        self.assertEqual(result["buffers_final"], buffer_state(13))
        self.assertFalse(result["base_kept"])
        self.assertIn("未通过", result["selected_validation"])

    def test_measurement_failure_disconnect_and_interrupt_restore_the_original_snapshot(self):
        for failure in (MODULE.TaskError("测速失败"), MODULE.TaskError("测速端心跳中断"), KeyboardInterrupt()):
            with self.subTest(failure=failure):
                result, output = self.exercise_flow(True, failure)
                self.assertEqual(result["buffer_trials"][0]["status"], "failed")
                self.assertEqual(result["buffer_trials"][0]["restored"], buffer_state(16))
                self.assertIn("已恢复调优前参数和队列", output)

    def test_partial_buffer_application_failure_restores_the_original_snapshot(self):
        result, output = self.exercise_flow(True, MODULE.TaskError("参数读回不一致"), "buffer")
        self.assertEqual(result["buffer_trials"][0]["status"], "failed")
        self.assertEqual(result["buffer_trials"][0]["restored"], buffer_state(16))

    def test_incomplete_snapshot_recovery_keeps_the_pending_record(self):
        result, output = self.exercise_flow(True, MODULE.TaskError("测速失败"), recovery_failure=True)
        self.assertIn("恢复或清理未完成，保留恢复记录", output)

    def test_silent_snapshot_restore_mismatch_is_not_reported_as_complete(self):
        result, output = self.exercise_flow(True, MODULE.TaskError("测速失败"), recovery_mismatch=True)
        self.assertIn("实际缓冲区与调优前不一致", result["recovery_error"])


if __name__ == "__main__":
    unittest.main()
