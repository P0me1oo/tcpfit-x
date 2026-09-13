#!/usr/bin/env python3
"""真实双端协议检查：需要 Linux root，只临时开放测试端口，不执行系统调优。"""
import argparse
import fcntl
import importlib.util
from pathlib import Path
import secrets
import shutil
import signal
import tempfile
import time
import types

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("tcpfit_return", ROOT / "tcpfit-return.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--control-port", type=int, default=45211)
    parser.add_argument("--iperf-port", type=int, default=45212)
    parser.add_argument("--family", type=int, choices=(4, 6), default=4)
    args = parser.parse_args()
    args.token_ttl = 300
    args.client_script = str(ROOT / "tcpfit-client.sh")
    with open("/var/lock/tcpfit.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run_dir = Path(tempfile.mkdtemp(prefix="tcpfit-peer-smoke-"))
        (run_dir / "measurements").mkdir()
        firewall = MODULE.Firewall(run_dir, args.family, args.control_port, args.iperf_port, secrets.token_hex(8))
        coordinator = MODULE.Coordinator(args, run_dir, run_dir, firewall)
        def cancel(signum, frame):
            raise MODULE.TaskError("协议检查已取消")
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, cancel)
        try:
            firewall.setup()
            MODULE.start_http(coordinator)
            print("在测速端完整复制以下命令执行（Linux / OpenWrt / iStoreOS 或 Windows PowerShell）：", flush=True)
            print(MODULE.join_command(args, coordinator.token), flush=True)
            while not coordinator.paired.wait(0.5):
                coordinator.check()
            idle = coordinator.measure_idle_latency(2)
            assert all(sample["mean_ms"] is not None for sample in idle)
            assert not coordinator.results
            print("空载延迟采集通过", flush=True)
            for streams in (1, 4):
                MODULE.request_measurement(types.SimpleNamespace(run_dir=str(run_dir), duration=4, streams=streams, stage="双端协议验证"))
                measured = coordinator.results[-1]
                assert measured["retransmits"] >= 0
                assert measured["receiver_mbps"] > 0
                print("有效空载/满载延迟样本: {} / {}".format(measured["latency"]["idle"]["samples"], measured["latency"]["loaded"]["samples"]), flush=True)
            coordinator.finished = "OK 双端协议验证通过"
            coordinator.done_ack.wait(5)
            print("双端认证、反向下载和结果回报通过", flush=True)
        finally:
            coordinator.close()
            MODULE.Firewall.cleanup(run_dir / "firewall.json")
            shutil.rmtree(str(run_dir))


if __name__ == "__main__":
    main()
