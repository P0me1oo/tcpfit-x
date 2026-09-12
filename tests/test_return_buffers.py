"""优化线路调优缓冲区试调回归：模拟速度、重传与异常，验证搜索和回滚。"""
import copy
from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import itertools
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
    def search(self, initial_rows, candidates, *, state=None, baseline=None, limit=64, rounds=8, can_trial=None):
        worker = BufferWorker(state or buffer_state())
        pending, stages, trials, search = list(candidates), [], [], {}
        output = io.StringIO()

        def measure(stage, modes=(1,)):
            stages.append((stage, modes, copy.deepcopy(worker.state)))
            self.assertTrue(pending, "不应增加未经安排的测速轮次")
            expected_mode, group = pending.pop(0)
            self.assertEqual(modes, (1,), "缓冲区试调只能测单连接")
            self.assertEqual(expected_mode, 1)
            return [row for row in group if row["streams"] == expected_mode]

        with tempfile.TemporaryDirectory() as directory, redirect_stdout(output), \
                mock.patch.object(MODULE, "BUFFER_TRIALS", rounds):
            path = Path(directory) / "buffer-trials.json"
            chosen_rows, chosen_state = MODULE.tune_buffers(
                worker, baseline or [row for row in initial_rows if row["streams"] == 1], initial_rows,
                copy.deepcopy(worker.state), limit * MIB,
                measure, trials, path, search, can_trial)
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved, trials)
        self.assertEqual(worker.state, chosen_state)
        self.assertFalse(pending, "应完成预期的候选验证")
        return worker, chosen_rows, chosen_state, trials, stages, output.getvalue(), search

    def test_good_single_connection_keeps_growing_until_three_rounds_without_gain(self):
        worker, _, state, trials, stages, output, search = self.search(rows(retrans=0), [
            (1, rows(single=speed, retrans=0)) for speed in (110, 120, 120, 119, 120)])
        self.assertEqual([trial["target_max_bytes"] for trial in trials], [value * MIB for value in (18, 20, 22, 24, 26)])
        self.assertEqual([trial["kept"] for trial in trials], [True, True, False, False, False])
        self.assertEqual(state, buffer_state(20))
        self.assertTrue(all(modes == (1,) for _, modes, _ in stages))
        self.assertIn("稳定提速", output)
        self.assertIn("连续 3 轮无收益", output)
        self.assertEqual(search["order"], [1])
        self.assertEqual(len(search["phases"]), 1)
        self.assertTrue(all(phase["goal_reached"] for phase in search["phases"]))

    def test_single_high_retransmission_only_adjusts_single_connection(self):
        initial = rows(retrans=1.6, four_retrans=6)
        worker, _, state, trials, stages, output, search = self.search(initial, [
            (1, rows(retrans=0.8, four_retrans=9)),
            (1, rows(retrans=0.3, four_retrans=10)),
            (1, rows(single=110, retrans=0.3, four_retrans=20)),
        ], rounds=3)
        self.assertEqual(worker.changes, [(14 * MIB, MIB), (12 * MIB, MIB), (15 * MIB, MIB)])
        self.assertEqual([trial["streams"] for trial in trials], [1, 1, 1])
        self.assertEqual([item[1] for item in stages], [(1,), (1,), (1,)])
        self.assertEqual(state, buffer_state(15))
        self.assertIn("本轮暂留", output)
        self.assertIn("1.600%", output)
        self.assertIn("16 MiB → 14 MiB", output)
        self.assertEqual(search["order"], [1])

    def test_four_connection_retransmission_does_not_change_single_connection_growth(self):
        worker, _, _, trials, stages, _, search = self.search(rows(retrans=0.4, four_retrans=40), [
            (1, rows(single=110, retrans=0.4, four_retrans=80))], rounds=1)
        self.assertEqual(worker.changes, [(18 * MIB, MIB)])
        self.assertTrue(trials[0]["kept"])
        self.assertEqual(stages[0][1], (1,))
        self.assertEqual(search["retrans_targets"], {"1": {"excellent": 0.5, "high": 1.0}})

    def test_no_four_connection_measurements_are_required(self):
        initial = [row for row in rows(retrans=1.6) if row["streams"] == 1]
        worker, measured, state, trials, stages, _, search = self.search(initial, [
            (1, rows(retrans=0.4)),
        ], rounds=1)
        self.assertEqual([trial["streams"] for trial in trials], [1])
        self.assertEqual(state, buffer_state(14))
        self.assertTrue(MODULE.base_decision(None, initial, measured)[0])

    def test_exact_thresholds_are_inclusive_and_high_thresholds_are_strict(self):
        baseline = rows(retrans=2, four_retrans=8)
        self.assertTrue(MODULE.mode_goal(baseline, rows(95, 190, retrans=0.5, four_retrans=1), 1))
        self.assertFalse(MODULE.mode_goal(baseline, rows(retrans=0.501), 1))
        for ratio, step in ((1, 1), (1.001, 2)):
            worker, _, _, _, _, _, _ = self.search(rows(retrans=ratio), [(1, rows(retrans=0.5))], rounds=1)
            self.assertEqual(worker.changes[0][0], (16 - step) * MIB)

    def test_speed_gain_requires_three_percent_and_nonoverlapping_samples(self):
        for previous, candidate, kept in (((100, 100), (103, 103), True),
                                           ((100, 100), (102.99, 102.99), False),
                                           ((95, 105), (101, 109), False),
                                           ((100, 100), (100, 112), False)):
            with self.subTest(previous=previous, candidate=candidate):
                initial, measured = rows(retrans=0), rows(retrans=0)
                for index in (0, 1):
                    initial[index]["receiver_mbps"] = previous[index]
                    measured[index]["receiver_mbps"] = candidate[index]
                _, _, state, trials, _, _, _ = self.search(initial, [(1, measured)], rounds=1)
                self.assertEqual(trials[0]["kept"], kept)
                self.assertEqual(state, buffer_state(18 if kept else 16))

    def test_speed_gain_cannot_hide_worse_retransmission(self):
        for ratio, kept in ((0.005, True), (0.02, False), (0.501, False)):
            with self.subTest(retransmission=ratio):
                _, _, state, trials, _, _, _ = self.search(rows(retrans=0), [
                    (1, rows(single=120, retrans=ratio))], rounds=1)
                self.assertEqual(trials[0]["kept"], kept)
                self.assertEqual(state, buffer_state(18 if kept else 16))

    def test_already_excellent_retransmission_improvement_cannot_replace_speed_gain(self):
        for speed in (96, 100, 102):
            with self.subTest(speed=speed):
                _, _, state, trials, _, _, _ = self.search(rows(retrans=0.4), [
                    (1, rows(single=speed, retrans=0.1))], rounds=1)
                self.assertFalse(trials[0]["kept"])
                self.assertEqual(state, buffer_state())

    def test_excellent_retransmission_with_low_speed_still_tries_growing(self):
        worker, _, state, trials, _, _, _ = self.search(rows(single=90, retrans=0), [
            (1, rows(single=94, retrans=0)), (1, rows(single=100, retrans=0))],
            baseline=rows(retrans=0), rounds=2)
        self.assertEqual([trial["target_max_bytes"] for trial in trials], [18 * MIB, 20 * MIB])
        self.assertEqual([trial["kept"] for trial in trials], [False, True])
        self.assertEqual(state, buffer_state(20))

    def test_growth_stops_at_ceiling_without_reversing(self):
        worker, _, state, trials, _, output, _ = self.search(rows(retrans=0), [
            (1, rows(single=110, retrans=0))], limit=17)
        self.assertEqual(worker.changes, [(17 * MIB, MIB)])
        self.assertEqual(state, buffer_state(17))
        self.assertIn("已达本机试调上限", output)
        worker, _, _, trials, _, _, _ = self.search(rows(), [], limit=16)
        self.assertFalse(worker.changes)
        self.assertFalse(trials)

    def test_high_retransmission_growth_refines_one_mib_below_rejected_candidate(self):
        worker, _, state, trials, _, output, _ = self.search(rows(retrans=0), [
            (1, rows(single=110, retrans=0)),
            (1, rows(single=120, retrans=1.6)),
            (1, rows(single=115, retrans=0)),
        ])
        self.assertEqual([trial["target_max_bytes"] for trial in trials], [value * MIB for value in (18, 20, 19)])
        self.assertEqual([trial["step_bytes"] for trial in trials], [2 * MIB, 2 * MIB, MIB])
        self.assertEqual([trial["kept"] for trial in trials], [True, False, True])
        self.assertEqual(trials[2]["refine_from_max_bytes"], 20 * MIB)
        self.assertEqual(worker.changes, [(18 * MIB, MIB), (20 * MIB, MIB), (18 * MIB, MIB), (19 * MIB, MIB)])
        self.assertEqual(state, buffer_state(19))
        self.assertIn("下调候选上限 20 MiB → 19 MiB，步长 1 MiB", output)
        self.assertIn("高重传边界的 1 MiB 微调已完成", output)

    def test_failed_fine_candidate_preserves_last_good_configuration_without_crossing_high_boundary(self):
        worker, _, state, trials, _, output, _ = self.search(rows(retrans=0), [
            (1, rows(single=110, retrans=0)),
            (1, rows(single=120, retrans=1.6)),
            (1, rows(single=115, retrans=1.2)),
        ])
        self.assertEqual([trial["target_max_bytes"] for trial in trials], [value * MIB for value in (18, 20, 19)])
        self.assertEqual([trial["kept"] for trial in trials], [True, False, False])
        self.assertEqual(state, buffer_state(18))
        self.assertIn("没有高于保留配置的未测候选", output)

    def test_fine_tuning_starts_only_above_high_retransmission_threshold(self):
        for ratio, expected in ((1.0, 20), (1.001, 17)):
            with self.subTest(retransmission=ratio):
                _, _, state, trials, _, _, _ = self.search(rows(retrans=0), [
                    (1, rows(single=110, retrans=ratio)), (1, rows(single=110, retrans=0))], rounds=2)
                self.assertEqual([trial["target_max_bytes"] for trial in trials], [18 * MIB, expected * MIB])
                self.assertEqual(state, buffer_state(expected))

    def test_fine_tuning_skips_visited_candidates_while_descending(self):
        _, _, state, trials, _, _, _ = self.search(rows(retrans=1.6), [
            (1, rows(retrans=0.8)),
            (1, rows(retrans=0.3)),
            (1, rows(single=110, retrans=1.2)),
            (1, rows(single=110, retrans=0.3)),
        ], state=buffer_state(24))
        self.assertEqual([trial["target_max_bytes"] for trial in trials], [value * MIB for value in (22, 20, 23, 21)])
        self.assertEqual(state, buffer_state(21))
        self.assertEqual(trials[-1]["refine_from_max_bytes"], 23 * MIB)
        self.assertEqual(trials[-1]["no_gain_rounds"], 0)

    def test_eight_round_limit_applies_to_single_connection(self):
        initial = rows(retrans=40)
        candidates = [(1, rows(retrans=40 - step * 4)) for step in range(1, 9)]
        worker, _, _, trials, _, output, search = self.search(initial, candidates, state=buffer_state(32))
        self.assertEqual(len(trials), 8)
        self.assertEqual(search["phases"][0]["status"], "stopped")
        self.assertEqual(len(search["phases"]), 1)
        self.assertIn("达到最大 8 轮", output)
        self.assertEqual([item[0] for item in worker.changes], [value * MIB for value in range(30, 15, -2)])

    def test_coarse_rejection_rolls_back_then_shrinks_step(self):
        worker, _, state, trials, _, output, _ = self.search(rows(retrans=6), [
            (1, rows(single=90, retrans=0.5)),
            (1, rows(retrans=0.5)),
        ], rounds=2)
        self.assertEqual(worker.changes, [(14 * MIB, MIB), (16 * MIB, MIB), (15 * MIB, MIB)])
        self.assertEqual([trial["kept"] for trial in trials], [False, True])
        self.assertEqual(state, buffer_state(15))
        self.assertIn("回退后缩小步长", output)

    def test_three_no_gain_rounds_stop_after_reverse_fine_tuning(self):
        initial = rows(retrans=6)
        worker, _, state, trials, _, output, _ = self.search(initial, [(1, initial)] * 3)
        self.assertEqual([trial["target_max_bytes"] for trial in trials], [14 * MIB, 15 * MIB, 17 * MIB])
        self.assertTrue(all(not trial["kept"] for trial in trials))
        self.assertEqual(state, buffer_state())
        self.assertIn("反向细调", output)
        self.assertIn("连续 3 轮无收益", output)

    def test_small_successive_speed_losses_cannot_accumulate_below_initial_reference(self):
        initial = rows(retrans=6)
        worker, _, state, trials, _, output, _ = self.search(initial, [
            (1, rows(single=96, retrans=4)),
            (1, rows(single=92.16, retrans=2)),
        ], rounds=2)
        self.assertEqual([trial["kept"] for trial in trials], [True, False])
        self.assertEqual(state, buffer_state(14))
        self.assertIn("比初值下降超过 5%", output)

    def test_single_connection_speed_regression_rejects_retransmission_gain(self):
        _, _, state, trials, _, _, _ = self.search(rows(retrans=6), [
            (1, rows(single=94, retrans=0.5))], rounds=1)
        self.assertFalse(trials[0]["kept"])
        self.assertEqual(state, buffer_state())

    def test_unstable_results_are_not_improvement(self):
        candidate = rows(retrans=0.5)
        candidate[0]["receiver_mbps"] = 50
        _, _, state, trials, _, output, _ = self.search(rows(retrans=6), [(1, candidate)], rounds=1)
        self.assertFalse(trials[0]["kept"])
        self.assertEqual(state, buffer_state())
        self.assertIn("不稳定", output)

    def test_failed_downward_trial_restores_defaults_as_well_as_maxima(self):
        worker, _, state, trials, stages, _, _ = self.search(
            rows(retrans=6), [(1, rows(single=90, retrans=0.5))], state=buffer_state(8, 8), rounds=1)
        self.assertEqual(stages[0][2], buffer_state(6, 6))
        self.assertEqual(worker.changes, [(6 * MIB, 6 * MIB), (8 * MIB, 8 * MIB)])
        self.assertEqual(state, buffer_state(8, 8))

    def test_time_reserve_stops_trials_before_mutation(self):
        worker, _, _, trials, _, output, _ = self.search(rows(retrans=6), [], can_trial=lambda: False)
        self.assertFalse(worker.changes)
        self.assertFalse(trials)
        self.assertIn("剩余时间", output)

    def test_growth_can_find_gain_after_rejected_candidates(self):
        worker, _, state, trials, _, _, _ = self.search(rows(retrans=0), [
            (1, rows(single=speed, retrans=0)) for speed in (100, 102, 105, 111)], rounds=4)
        self.assertEqual([trial["target_max_bytes"] for trial in trials], [value * MIB for value in (18, 20, 22, 24)])
        self.assertEqual([trial["kept"] for trial in trials], [False, False, True, True])
        self.assertEqual(state, buffer_state(24))
        self.assertEqual(worker.changes[:4], [(18 * MIB, MIB), (16 * MIB, MIB), (20 * MIB, MIB), (16 * MIB, MIB)])

    def test_visited_neighbors_and_bounds_do_not_repeat_candidates(self):
        visited = {16 * MIB, 18 * MIB, 20 * MIB}
        self.assertEqual(MODULE.next_buffer_target(18 * MIB, 24 * MIB, 1, 2 * MIB, visited), (19 * MIB, 1, MIB))
        visited.update({17 * MIB, 19 * MIB})
        self.assertIsNone(MODULE.next_buffer_target(18 * MIB, 24 * MIB, 1, 2 * MIB, visited))
        self.assertEqual(MODULE.next_buffer_target(6 * MIB, 8 * MIB, -1, MIB, {6 * MIB}), (7 * MIB, 1, MIB))
        self.assertEqual(MODULE.next_buffer_target(7 * MIB, 8 * MIB, -1, 2 * MIB, {7 * MIB}), (6 * MIB, -1, MIB))
        limit = 20 * MIB + MIB // 2
        self.assertEqual(MODULE.next_buffer_growth_target(18 * MIB, limit, {18 * MIB, 19 * MIB, 20 * MIB}),
                         (limit, 1, 2 * MIB + MIB // 2))
        self.assertIsNone(MODULE.next_buffer_growth_target(18 * MIB, limit, {19 * MIB, 20 * MIB, limit}))
        self.assertEqual(MODULE.next_buffer_refine_target(20 * MIB, 16 * MIB, {18 * MIB, 19 * MIB, 20 * MIB}),
                         (17 * MIB, -1, 3 * MIB))
        self.assertIsNone(MODULE.next_buffer_refine_target(20 * MIB, 18 * MIB, {19 * MIB, 20 * MIB}))
        self.assertIsNone(MODULE.next_buffer_refine_target(6 * MIB + MIB // 2, 6 * MIB, set()))

    def test_trial_cannot_start_below_six_mib(self):
        with self.assertRaisesRegex(MODULE.TaskError, "试调范围"):
            self.search(rows(retrans=6), [], state=buffer_state(4))

    def test_missing_or_inconsistent_actual_buffer_values_are_rejected(self):
        with self.assertRaisesRegex(MODULE.TaskError, "缓冲区参数"):
            MODULE.buffers_from_sysctl({})
        state = buffer_state(16)
        state["net.core.wmem_max"] = [15 * MIB]
        with self.assertRaisesRegex(MODULE.TaskError, "实际上限"):
            MODULE.check_buffer_target(state, 16 * MIB)

    def test_measurement_failure_and_interrupt_restore_previous_buffers(self):
        for failure in (MODULE.TaskError("测速失败"), KeyboardInterrupt()):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
                worker = BufferWorker(buffer_state())
                trials = []
                measure = mock.Mock(side_effect=failure)
                with self.assertRaises(type(failure)):
                    MODULE.tune_buffers(worker, rows(retrans=6), rows(retrans=6),
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

    def test_return_worker_uses_bdp_plus_two_mib_and_six_mib_floor(self):
        for ram, bandwidth, rtt, expected_max in ((2048, 1000, 180, 22500000 + 2 * MIB),
                                                  (512, 1000, 180, 22500000 + 2 * MIB),
                                                  (512, 1000, 200, 24 * MIB),
                                                  (512, 200, 100, 6 * MIB),
                                                  (256, 10, 1, 6 * MIB),
                                                  (192, 1000, 180, 6 * MIB)):
            with self.subTest(ram=ram, bandwidth=bandwidth):
                script = MODULE.WORKER.replace('LOCK_HELD=1', 'LOCK_HELD=1\ndetect_ram_mb(){ echo ' + str(ram) + '; }')
                result = subprocess.run([shutil.which("bash") or "bash", "-c", script, "return-profile",
                                         (ROOT / "tcpfit.sh").as_posix(), "buffer-plan", "proxy", str(bandwidth), str(rtt)],
                                        capture_output=True, text=True, encoding="utf-8", timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                profile = list(map(int, result.stdout.split()))
                self.assertEqual(profile[0], bandwidth * rtt * 125)
                self.assertEqual(profile[1], expected_max)

    def test_return_memory_ceiling_below_floor_stops_before_tuning(self):
        for action in ("buffer-plan", "tune"):
            with self.subTest(action=action):
                script = MODULE.WORKER.replace('LOCK_HELD=1',
                    'LOCK_HELD=1\ndetect_ram_mb(){ echo 128; }\ncmd_tune(){ echo unexpected-tune; }')
                result = subprocess.run([shutil.which("bash") or "bash", "-c", script, "return-profile",
                                         (ROOT / "tcpfit.sh").as_posix(), action, "proxy", "10", "1"],
                                        capture_output=True, text=True, encoding="utf-8", timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("低于 6 MiB 下限", result.stderr)
                self.assertEqual(result.stdout, "")

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
    def exercise_flow(self, candidate_passes_reference, failure=None, failure_action="sample",
                      recovery_failure=False, recovery_mismatch=False, selection=None,
                      old_rate=None, shape_passes=True, server_bw=1000, client_bw=200,
                      probe_speeds=None, expected_error=None, excellent_initial=False, growth_high=False,
                      initial_speeds=None, idle_means=(100, 100), final_regression=False, flow_rate=None):
        probe_values = iter(probe_speeds) if probe_speeds is not None else None
        initial_values = iter(initial_speeds) if initial_speeds is not None else None
        self.flow_actions = []
        flow_actions = self.flow_actions
        task_failed = failure is not None or expected_error is not None
        paired = threading.Event()
        paired.set()
        coordinator = types.SimpleNamespace(peer="192.0.2.1", token="test-pairing-placeholder", paired=paired,
                                            done_ack=paired, results=[], started=time.monotonic(), check=mock.Mock(), close=mock.Mock(), fail=mock.Mock())

        def measure_idle_latency(repeats):
            self.assertEqual(repeats, 2)
            flow_actions.append(("latency",))
            if failure is not None and failure_action == "latency":
                raise failure
            return [{"mean_ms": value} for value in idle_means]

        coordinator.measure_idle_latency = measure_idle_latency

        class FlowWorker(BufferWorker):
            def run(self, action, *args, **kwargs):
                flow_actions.append((action,) + args)
                if action == "profile":
                    return "eth0\n100\n10\n90\n75", 0
                if action == "margin":
                    return "5", 0
                if action == "keys":
                    return "\n".join(MODULE.BUFFER_KEYS), 0
                if action == "buffer-plan":
                    return "{} {} {} {}".format(14 * MIB, 16 * MIB, MIB, 20 * MIB), 0
                if action == "tune":
                    self.state = buffer_state(16)
                    self.congestion = "bbr"
                    return "", 0
                if action == "buffer" and failure is not None and failure_action == "buffer" and int(args[0]) == 14 * MIB:
                    super().run(action, *args, **kwargs)
                    raise failure
                if action == "sample":
                    maximum = self.state["net.ipv4.tcp_rmem"][2] // MIB
                    if failure is not None and failure_action == "sample" and maximum == 14:
                        raise failure
                    speed = {8: (100, 200), 16: (100, 210), 14: (99, 205)}
                    ratios = {8: (0.8, 6), 16: (1.6, 6), 14: (0.3, 0.8)}
                    if excellent_initial:
                        speed.update({16: (99, 200), 18: (106, 200), 19: (110, 200), 20: (112, 200)})
                        ratios.update({value: (0, 0) for value in speed})
                        if growth_high:
                            ratios[20] = (1.6, 0)
                    else:
                        speed.update({value: (99, 205) for value in (15, 17, 18)})
                        ratios.update({value: (0.3, 0.8) for value in (15, 17, 18)})
                    group = rows(*speed[maximum], retrans=ratios[maximum][0], four_retrans=ratios[maximum][1])
                    sample = next(row for row in group if row["streams"] == args[0])
                    if self.stage.startswith("路径带宽探测"):
                        sample["latency"]["idle"]["mean_ms"] = 999
                        if probe_values is not None:
                            sample["receiver_mbps"] = next(probe_values)
                    if self.stage.startswith("初值测速") and initial_values is not None:
                        sample["receiver_mbps"] = next(initial_values)
                    if maximum == 14 and not candidate_passes_reference and "试调" in self.stage and args[0] == 1:
                        sample["receiver_mbps"] = 90
                    if final_regression and self.stage.startswith("最终配置验证") and args[0] == 1:
                        sample["receiver_mbps"] = 90
                    if args[0] == 4 and self.queue_rate is not None and not shape_passes:
                        sample["receiver_mbps"] = old_rate - 1
                    coordinator.results.append(sample)
                    return "", 0
                if action == "band":
                    return "0", 0
                if action == "spike":
                    return "", 1
                if action == "fq":
                    self.queue_rate = None
                    self.flow_rate = None
                    return "", 0
                if action in ("test-shape", "shape"):
                    self.queue_rate = int(args[-1])
                    return "", 0
                if action in ("snapshot", "archive"):
                    return "", 0
                return super().run(action, *args, **kwargs)

        worker = FlowWorker(buffer_state(8))
        worker.congestion = "cubic"
        worker.queue_rate = old_rate
        worker.flow_rate = flow_rate

        def snapshot(*args):
            return {"sysctl": dict({key: " ".join(map(str, values)) for key, values in worker.state.items()},
                                   **{"net.ipv4.tcp_congestion_control": worker.congestion}),
                    "queue": {"rate": worker.queue_rate, "limited_fq": worker.flow_rate is not None,
                              "qdiscs": [{"kind": "fq", "options": ["maxrate", worker.flow_rate] if worker.flow_rate else []}]},
                    "service_active": False,
                    "files": {}, "routes": {}}

        def restore(saved):
            if recovery_failure:
                raise MODULE.TaskError("模拟完整快照暂时无法恢复")
            worker.state = MODULE.buffers_from_sysctl(saved["sysctl"])
            worker.congestion = saved["sysctl"]["net.ipv4.tcp_congestion_control"]
            worker.queue_rate = saved["queue"]["rate"]
            options = saved["queue"]["qdiscs"][0]["options"]
            worker.flow_rate = options[options.index("maxrate") + 1] if "maxrate" in options else None
            if recovery_mismatch:
                worker.state["net.core.wmem_max"] = [9 * MIB]

        with tempfile.TemporaryDirectory() as directory:
            task_dir = Path(directory)
            args = types.SimpleNamespace(script=str(ROOT / "tcpfit.sh"), client_script=str(ROOT / "tcpfit-client.sh"),
                                         state_dir=str(task_dir / "state"), lock_fd=9, role="proxy", family=4,
                                         server="192.0.2.2", server_bw=server_bw, client_bw=client_bw, token_ttl=600,
                                         control_port=0, iperf_port=0, repeats=2, yes=selection is None)
            coordinator.run_dir = task_dir / "runtime"
            firewall = mock.Mock(state={"backend": "none", "manager": None}, path=task_dir / "firewall.json")
            guardian = mock.Mock()
            output = io.StringIO()

            def private_path(value):
                if str(value) == "/proc/meminfo":
                    raise AssertionError("优化线路调优不应读取可用内存或检查预留余量")
                return task_dir / "runtime" if str(value) == "/run/tcpfit" else Path(value)

            selector = MODULE.select_configuration

            def choose(book, recommendation, automatic, **kwargs):
                return selector(book, recommendation, automatic, reader=lambda: str(selection))

            def restore_queue(queue):
                worker.queue_rate = queue["rate"]
                options = queue["qdiscs"][0]["options"]
                worker.flow_rate = options[options.index("maxrate") + 1] if "maxrate" in options else None

            with ExitStack() as stack:
                for patch in (
                    mock.patch.object(MODULE, "validate_environment"),
                    mock.patch.object(MODULE, "Path", side_effect=private_path),
                    mock.patch.object(MODULE, "process_stamp", return_value="test-process"),
                    mock.patch.object(MODULE.subprocess, "Popen", return_value=guardian),
                    mock.patch.object(MODULE, "Coordinator", return_value=coordinator),
                    mock.patch.object(MODULE, "Worker", return_value=worker),
                    mock.patch.object(MODULE, "Firewall", return_value=firewall),
                    mock.patch.object(MODULE, "start_http"),
                    mock.patch.object(MODULE.signal, "SIGHUP", 1, create=True),
                    mock.patch.object(MODULE.signal, "signal", return_value=0),
                    mock.patch.object(MODULE.time, "monotonic", side_effect=itertools.count(time.monotonic(), 1)),
                    mock.patch.object(MODULE.time, "sleep"),
                    mock.patch.object(MODULE, "command", return_value=subprocess.CompletedProcess([], 0, "192.0.2.1 dev eth0", "")),
                    mock.patch.object(MODULE.QueueState, "capture"),
                    mock.patch.object(MODULE.QueueState, "restore", side_effect=restore_queue),
                    mock.patch.object(MODULE.Snapshot, "capture", side_effect=snapshot),
                    mock.patch.object(MODULE, "select_configuration", side_effect=choose),
                    redirect_stdout(output),
                ):
                    stack.enter_context(patch)
                restore_call = stack.enter_context(mock.patch.object(MODULE.Snapshot, "restore", side_effect=restore))
                persist_call = stack.enter_context(mock.patch.object(MODULE, "persist_selected_configuration"))
                code = MODULE.run_locked_task(args)
            self.assertEqual(code, 2 if task_failed else 0, output.getvalue())
            record = next((task_dir / "state" / "return").glob("*/result.json"))
            result = json.loads(record.read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "failed" if task_failed else "completed")
            if expected_error is not None:
                self.assertIn(expected_error, result["error"])
            incomplete = recovery_failure or recovery_mismatch
            self.assertEqual(bool(result.get("cleanup_complete")), not incomplete)
            self.assertEqual((task_dir / "state" / "return-pending.json").exists(), incomplete)
            self.assertEqual(restore_call.call_count, int(task_failed or final_regression)
                             + (1 if selection is not None and result.get("selected_config") != result.get("recommended_config") else 0))
            if incomplete:
                self.assertIn("recovery_error", result)
            elif task_failed:
                self.assertEqual(worker.state, buffer_state(8))
                self.assertEqual(result["buffers_final"], buffer_state(8))
                self.assertFalse(result["base_kept"])
                final = json.loads(record.with_name("final.json").read_text(encoding="utf-8"))
                self.assertEqual(MODULE.buffers_from_sysctl(final["sysctl"]), buffer_state(8))
            else:
                self.assertEqual(result["buffers_final"], worker.state)
                if selection is not None and result["selected_config"] != result["recommended_config"]:
                    saved = json.loads(record.with_name("final.json").read_text(encoding="utf-8"))
                    self.assertEqual(saved["sysctl"]["net.ipv4.tcp_congestion_control"],
                                     "cubic" if selection == 1 and server_bw is None and client_bw is None else "bbr")
                    persist_call.assert_called_once()
            return result, output.getvalue()

    def test_complete_flow_applies_initial_values_before_testing_and_reuses_retained_measurements(self):
        result, output = self.exercise_flow(True)
        self.assertTrue(result["base_kept"])
        self.assertEqual(result["buffers_final"], buffer_state(14))
        self.assertEqual(result["final"], result["after"])
        self.assertNotEqual(result["after"], result["initial_after"])
        trial_numbers = {row["number"] for trial in result["buffer_trials"] for row in trial["measurements"]}
        self.assertTrue(trial_numbers.issuperset(row["number"] for row in result["final"]))
        self.assertEqual(result["after"], result["stage_measurements"])
        self.assertNotIn("before", result)
        self.assertEqual(result["speed_reference"], "initial_after")
        self.assertEqual(result["measurements"][0]["stage"], "初值测速")
        self.assertEqual(result["measurements"][0]["buffers"], buffer_state(16))
        self.assertFalse(any("验收" in row["stage"] or "原始基线" in row["stage"] for row in result["measurements"]))
        actions = [entry[0] for entry in self.flow_actions]
        self.assertLess(actions.index("latency"), actions.index("buffer-plan"))
        self.assertLess(actions.index("buffer-plan"), actions.index("tune"))
        self.assertLess(actions.index("tune"), actions.index("sample"))
        self.assertEqual(result["buffer_plan"]["bdp_bytes"] + 2 * MIB, result["buffer_plan"]["max_bytes"])
        self.assertEqual(len(result["final"]), 2)
        self.assertTrue(all(row["streams"] == 1 for row in result["measurements"]))
        self.assertFalse(any("带宽探测" in row["stage"] for row in result["measurements"]))
        self.assertNotIn("path_bandwidth", result)
        self.assertEqual(result["reference_bandwidth"], 200)
        self.assertEqual(result["rtt_ms"], 100)
        self.assertIn("最终缓冲区：接收 14 MiB / 发送 14 MiB", output)
        self.assertEqual(result["buffer_search"]["max_rounds"], 8)
        self.assertIn("停止原因", output)
        self.assertNotIn("起始值", output)
        self.assertNotIn("可用内存", output)
        self.assertEqual([trial["target_max_bytes"] for trial in result["buffer_trials"]],
                         [value * MIB for value in (14, 15)])

    def test_excellent_initial_values_grow_and_reuse_the_faster_configuration_results(self):
        result, output = self.exercise_flow(True, excellent_initial=True)
        self.assertTrue(result["base_kept"])
        self.assertEqual(result["buffers_final"], buffer_state(20))
        self.assertEqual([trial["target_max_bytes"] for trial in result["buffer_trials"]],
                         [value * MIB for value in (18, 20)])
        self.assertEqual([trial["kept"] for trial in result["buffer_trials"]], [True, True])
        self.assertEqual([row["receiver_mbps"] for row in result["final"]], [112, 112])
        self.assertEqual(result["final"], result["buffer_trials"][-1]["measurements"])
        self.assertIn("稳定提速", output)

    def test_complete_flow_validates_fine_candidate_below_high_retransmission_boundary(self):
        result, output = self.exercise_flow(True, excellent_initial=True, growth_high=True)
        self.assertTrue(result["base_kept"])
        self.assertEqual(result["buffers_final"], buffer_state(19))
        self.assertEqual([trial["target_max_bytes"] for trial in result["buffer_trials"]],
                         [value * MIB for value in (18, 20, 19)])
        self.assertEqual(result["buffer_trials"][-1]["refine_from_max_bytes"], 20 * MIB)
        self.assertEqual([row["receiver_mbps"] for row in result["final"]], [110, 110])
        self.assertIn("下调候选上限 20 MiB → 19 MiB", output)

    def test_trial_speed_guard_uses_the_initial_configuration_results(self):
        result, output = self.exercise_flow(False)
        self.assertTrue(result["base_kept"])
        self.assertFalse(result["buffer_trials"][0]["kept"])
        self.assertEqual(result["buffers_final"], buffer_state(15))
        self.assertEqual(result["final"], result["after"])
        self.assertIn("比初值下降超过 5%", output)

    def test_queue_change_still_validates_single_connection_and_rolls_back_regression(self):
        result, output = self.exercise_flow(True, old_rate=150, final_regression=True)
        self.assertFalse(result["base_kept"])
        self.assertEqual(result["buffers_final"], buffer_state(8))
        self.assertEqual(result["final_rate"], 150)
        self.assertIn("已恢复原配置", result["recommended_validation"])

    def test_idle_latency_is_measured_before_bdp_and_falls_back_explicitly(self):
        for means, expected, source in (((100.2, 102.2), 102, "实测"), ((None, None), 100, "默认估值")):
            with self.subTest(means=means):
                result, output = self.exercise_flow(True, idle_means=means)
                self.assertEqual(result["rtt_ms"], expected)
                self.assertIn(source, result["rtt_source"])
                self.assertEqual(next(entry for entry in self.flow_actions if entry[0] == "buffer-plan")[-1], expected)

    def test_unstable_or_missing_initial_results_stop_and_restore_the_original_snapshot(self):
        for speeds in ((100, 200), (100, None)):
            with self.subTest(speeds=speeds):
                result, _ = self.exercise_flow(True, initial_speeds=speeds, expected_error="初值测速不能用于速度比较")
                self.assertNotIn("buffer_trials", result)
                self.assertEqual(result["buffers_final"], buffer_state(8))

    def test_latency_failure_stops_before_applying_initial_values(self):
        result, _ = self.exercise_flow(True, MODULE.TaskError("延迟采集失败"), failure_action="latency")
        self.assertFalse(any(entry[0] == "tune" for entry in self.flow_actions))
        self.assertEqual(result["buffers_final"], buffer_state(8))

    def test_empty_bandwidth_probes_four_connections_then_tunes_only_single(self):
        for speed, expected in ((19.4, 19), (123, 120), (230, 250)):
            with self.subTest(speed=speed):
                result, output = self.exercise_flow(True, server_bw=None, client_bw=None, probe_speeds=(speed, speed))
                probe = [row for row in result["measurements"] if row["stage"] == "路径带宽探测"]
                self.assertEqual([row["streams"] for row in probe], [4, 4])
                self.assertTrue(all(row["streams"] == 1 for row in result["measurements"] if row not in probe))
                self.assertEqual(result["reference_bandwidth"], expected)
                self.assertEqual(result["path_bandwidth"], expected)
                self.assertEqual(result["bandwidth_source"], "四连接实测路径带宽")
                self.assertEqual(result["rtt_ms"], 100)
                self.assertEqual(result["buffers_final"], buffer_state(14))
                self.assertIn("四连接实测路径带宽", output)

    def test_either_nominal_bandwidth_skips_probe_in_complete_flow(self):
        for server, client, expected in ((1000, None, 1000), (None, 200, 200)):
            with self.subTest(server=server, client=client):
                result, _ = self.exercise_flow(True, server_bw=server, client_bw=client)
                self.assertNotIn("path_bandwidth", result)
                self.assertEqual(result["reference_bandwidth"], expected)
                self.assertTrue(all(row["streams"] == 1 for row in result["measurements"]))

    def test_invalid_probe_restores_original_configuration_before_buffer_tuning(self):
        for speeds, reason in (((100, 200), "不稳定或不完整"), ((None, 200), "不稳定或不完整"),
                               ((0.2, 0.2), "低于可推导范围")):
            with self.subTest(speeds=speeds):
                result, _ = self.exercise_flow(True, server_bw=None, client_bw=None,
                                               probe_speeds=speeds, expected_error=reason)
                self.assertNotIn("buffer_trials", result)
                self.assertEqual(result["buffers_final"], buffer_state(8))

    def test_manual_sequence_restores_full_configuration_instead_of_recommended_buffers(self):
        result, output = self.exercise_flow(True, selection=1, server_bw=None, client_bw=None)
        self.assertEqual(result["selected_number"], 1)
        self.assertNotEqual(result["selected_config"], result["recommended_config"])
        self.assertEqual(result["buffers_final"], buffer_state(8))
        self.assertTrue(all(row["config_id"] == result["selected_config"] for row in result["final"]))
        self.assertIn("手动选择", result["selected_validation"])

    def test_manual_selection_uses_configuration_number_and_can_save_a_rejected_trial(self):
        result, output = self.exercise_flow(True, selection=3)
        self.assertEqual(result["selected_number"], 3)
        self.assertEqual(result["recommended_number"], 2)
        self.assertEqual(result["buffers_final"], buffer_state(15))
        self.assertTrue(result["base_kept"])
        self.assertIn("已回退", result["selected_validation"])
        self.assertEqual(result["configuration_options"][2]["measurement_numbers"], [5, 6])
        self.assertIn("已选择配置 3", output)

    def test_existing_shaper_keeps_four_connection_checks_and_single_connection_final_guard(self):
        result, _ = self.exercise_flow(True, old_rate=150)
        self.assertGreater(result["final_rate"], 150)
        for key in ("initial_after", "after"):
            self.assertEqual([row["streams"] for row in result[key]], [1, 1])
        for key in ("shape_reference", "shape_measurements"):
            self.assertEqual([row["streams"] for row in result[key]], [4, 4])
            self.assertTrue(all(row["buffers"] == buffer_state(14) for row in result[key]))
        self.assertEqual([row["streams"] for row in result["final"]], [1, 1, 4, 4])
        self.assertFalse(any("带宽探测" in row["stage"] for row in result["measurements"]))

    def test_rate_limit_is_reported_before_removal_and_unlimited_is_not_mislabelled(self):
        for rate, flow_rate, expected in ((150, None, "总限速 150 Mbps"),
                                          (None, "1Gbit", "单连接限速 1000 Mbps"),
                                          (None, None, "未设置")):
            with self.subTest(rate=rate, flow_rate=flow_rate):
                result, output = self.exercise_flow(True, old_rate=rate, flow_rate=flow_rate)
                self.assertEqual(result["rate_limits_before"], expected)
                self.assertIn("当前限速：" + expected, output)
                if rate is not None or flow_rate is not None:
                    self.assertLess(output.index("当前限速：" + expected), output.index("临时解除限速"))
                else:
                    self.assertNotIn("临时解除限速", output)

    def test_failed_four_connection_shaper_validation_restores_old_rate(self):
        result, _ = self.exercise_flow(True, old_rate=150, shape_passes=False)
        self.assertEqual(result["final_rate"], 150)
        self.assertTrue(result["base_kept"])
        self.assertEqual(result["buffers_final"], buffer_state(14))
        self.assertIn("候选未通过", result["shape_reason"])
        self.assertEqual([row["streams"] for row in result["final"]], [1, 1])

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
