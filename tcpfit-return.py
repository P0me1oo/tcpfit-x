#!/usr/bin/env python3
"""回国调优的接入、测量和事务管理；网络参数由 tcpfit.sh 的公共函数应用。"""

import argparse
import base64
from contextlib import contextmanager
import hmac
import http.server
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import socket
import socketserver
import statistics
import subprocess
import sys
import tempfile
import threading
import time

VERSION = "0.6.0"
MAX_BODY = 2 * 1024 * 1024
HEARTBEAT_TIMEOUT = 45
TASK_TIMEOUT = 1800
CONFIG_FILES = (
    "/etc/sysctl.d/99-tcpfit.conf",
    "/usr/local/sbin/tcpfit-qdisc.sh",
    "/etc/systemd/system/tcpfit-qdisc.service",
    "/etc/networkd-dispatcher/routable.d/50-tcpfit-initcwnd",
    "/etc/modules-load.d/tcpfit-bbr.conf",
    "/etc/systemd/system/multi-user.target.wants/tcpfit-qdisc.service",
)


class TaskError(Exception):
    pass


def log(message):
    print("[*] " + str(message), flush=True)


def atomic_json(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + "." + secrets.token_hex(4))
    with open(str(tmp), "x", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(path))


def read_json(path):
    with open(str(path), encoding="utf-8") as source:
        return json.load(source)


def command(args, check=True, input_data=None):
    try:
        result = subprocess.run(
            [str(arg) for arg in args], input=input_data,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
            universal_newlines=True, encoding="utf-8", errors="replace",
            env=dict(os.environ, LC_ALL="C"),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise TaskError("命令无法完成: {}: {}".format(args[0], error))
    if check and result.returncode:
        raise TaskError("命令失败: {}: {}".format(" ".join(str(arg) for arg in args), result.stderr.strip()))
    return result


def process_stamp(pid):
    try:
        # comm 字段可能含空格或括号；从最后一个右括号之后取 starttime。
        fields = Path("/proc/{}/stat".format(pid)).read_text().rsplit(")", 1)[1].split()
        return fields[19] if fields[0] != "Z" else None
    except (OSError, IndexError):
        return None


def stop_process(entry):
    if not entry or process_stamp(entry["pid"]) != entry["stamp"]:
        return
    try:
        os.killpg(entry["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 3
        while process_stamp(entry["pid"]) == entry["stamp"] and time.monotonic() < deadline:
            time.sleep(0.1)
        if process_stamp(entry["pid"]) == entry["stamp"]:
            os.killpg(entry["pid"], signal.SIGKILL)
    except ProcessLookupError:
        pass


def positive(value, name, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TaskError("测量数据不完整: {} 必须是有限数值".format(name))
    if value < 0 or (not allow_zero and value == 0):
        raise TaskError("测量数据无效: {}".format(name))
    return value


@contextmanager
def task_lock(inherited=None):
    """所有会修改网络状态的入口共用一把锁；守护进程继承同一打开文件描述符。"""
    if not hasattr(os, "geteuid") or os.geteuid() != 0 or not sys.platform.startswith("linux"):
        raise TaskError("调优和恢复需要常规 Linux 的 root 权限")
    import fcntl
    path = Path("/var/lock/tcpfit.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    owned = open(str(path), "a") if inherited is None else None
    fd = owned.fileno() if owned else inherited
    try:
        actual, expected = os.fstat(fd), path.stat()
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise TaskError("继承的描述符不是 tcpfit 任务锁")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, ValueError):
            raise TaskError("已有 tcpfit 任务正在运行，不能重复启动、恢复或抢占")
        yield fd
    finally:
        if owned:
            owned.close()


def parse_measurement(client, server, streams, duration):
    """只接受完整 TCP 反向测试，接收吞吐取家宽端，重传取服务器发送端。"""
    for label, document in (("测速端", client), ("调优端", server)):
        if not isinstance(document, dict) or document.get("error"):
            raise TaskError("{} iperf3 失败: {}".format(label, document.get("error", "结果格式错误") if isinstance(document, dict) else "结果格式错误"))
        if not isinstance(document.get("start"), dict) or not isinstance(document.get("end"), dict):
            raise TaskError("测量数据不完整: 缺少 start/end 对象")
        start = document["start"].get("test_start")
        if not isinstance(start, dict) or not isinstance(document["end"].get("streams"), list):
            raise TaskError("测量数据不完整: 缺少测试参数或连接列表")
        if start.get("protocol") != "TCP" or start.get("reverse") != 1 or start.get("num_streams") != streams:
            raise TaskError("测量方向或连接数不符，只接受指定连接数的 TCP -R 测试")
        if start.get("bidir") or start.get("omit", 0) != 0 or start.get("duration") != duration:
            raise TaskError("测试时长、预热或双向参数与本轮任务不符")
        if len(document.get("end", {}).get("streams", [])) != streams:
            raise TaskError("测量数据不完整: 缺少连接结果")
    sent = server.get("end", {}).get("sum_sent", {})
    received = client.get("end", {}).get("sum_received", {})
    for name, summary in (("发送", sent), ("接收", received)):
        if not isinstance(summary, dict):
            raise TaskError("测量数据不完整: {}汇总格式错误".format(name))
        for field in ("bits_per_second", "bytes", "seconds"):
            positive(summary.get(field), name + field)
        # 允许结束握手的时间差；明显不足一个完整测试周期的数据不参与调参。
        if summary["seconds"] < duration - 0.5 or summary["seconds"] > duration + 15:
            raise TaskError("测量数据不完整: {}时长与任务不符".format(name))
        actual = summary["bytes"] * 8 / summary["seconds"]
        if abs(actual - summary["bits_per_second"]) > max(1, actual * 0.01):
            raise TaskError("测量数据无效: {}字节数与吞吐不一致".format(name))
    retrans = positive(sent.get("retransmits"), "服务器重传次数", True)
    if int(retrans) != retrans:
        raise TaskError("测量数据无效: 重传次数不是整数")
    if received["bytes"] > sent["bytes"]:
        raise TaskError("测量数据不一致: 接收字节数超过发送字节数")
    if client["start"].get("cookie") != server["start"].get("cookie"):
        raise TaskError("测量数据不属于同一次 iperf3 连接")
    return {
        "streams": streams, "requested_seconds": duration,
        "sender_mbps": sent["bits_per_second"] / 1e6,
        "receiver_mbps": received["bits_per_second"] / 1e6,
        "sender_bytes": sent["bytes"], "receiver_bytes": received["bytes"],
        "sender_seconds": sent["seconds"], "receiver_seconds": received["seconds"],
        "retransmits": int(retrans),
        "estimated_retrans_pct": retrans * 100 * 1448 / sent["bytes"],
    }


def latency_summary(text):
    values = []
    for line in text.splitlines():
        try:
            connect, lookup = map(float, line.split())
        except ValueError:
            continue
        value = (connect - lookup) * 1000
        if math.isfinite(value) and 0 < value <= 10000:
            values.append(value)
    return {
        "method": "tcp_connect", "samples": len(values), "values_ms": values,
        "mean_ms": statistics.mean(values) if len(values) >= 3 else None,
        "reason": None if len(values) >= 3 else "有效 TCP 握手样本不足 3 个",
    }


class QueueState:
    """按 tc 的可重放参数保存队列；不猜测未知结构的恢复方法。"""

    OPTIONS = {
        "fq": {"limit", "flow_limit", "buckets", "orphan_mask", "quantum", "initial_quantum", "maxrate", "low_rate_threshold", "refill_delay", "timer_slack", "horizon", "ce_threshold", "weights", "priomap"},
        "fq_codel": {"limit", "flows", "quantum", "target", "interval", "memory_limit", "ce_threshold", "drop_batch"},
        "pfifo_fast": {"bands", "priomap"},
        "htb": {"default", "r2q", "direct_qlen"},
        "mq": set(), "noqueue": set(),
    }
    FLAGS = {"ecn", "noecn", "pacing", "nopacing", "horizon_drop", "horizon_cap", "offload"}
    CLASS_OPTIONS = {"prio", "rate", "ceil", "burst", "cburst", "quantum", "linklayer", "mpu", "overhead"}

    @staticmethod
    def size_bytes(value):
        match = re.fullmatch(r"([0-9.]+)([KMG]?)[bB]?", value)
        if not match:
            raise TaskError("无法解析队列字节数: " + value)
        return str(int(float(match[1]) * {"": 1, "K": 1024, "M": 1048576, "G": 1073741824}[match[2]]))

    @classmethod
    def parse_options(cls, kind, words):
        output, index = [], 0
        while index < len(words):
            key = words[index]
            index += 1
            if key in ("refcnt", "direct_packets_stat"):
                index += 1
                continue
            if key in cls.FLAGS:
                if key == "offload":
                    raise TaskError("暂不支持恢复硬件卸载队列，未修改网络参数")
                output.append(key)
                continue
            if key not in cls.OPTIONS[kind] or index >= len(words):
                raise TaskError("无法可靠恢复队列参数 {} {}，未修改网络参数".format(kind, key))
            count = 16 if key == "priomap" else (3 if key == "weights" else 1)
            values = words[index:index + count]
            if len(values) != count:
                raise TaskError("队列参数不完整")
            index += count
            # tc 的显示后缀 p 不是所有版本的输入解析器都接受。
            if key in ("limit", "flow_limit", "flows", "buckets"):
                values = [re.sub(r"p$", "", value) for value in values]
            if key in ("quantum", "initial_quantum"):
                values = [cls.size_bytes(value) for value in values]
            output.extend([key] + values)
        # tc 在关闭 ECN 时省略该标志，重建 fq_codel 的默认值却是开启。
        if kind == "fq_codel" and "ecn" not in output and "noecn" not in output:
            output.append("noecn")
        return output

    @classmethod
    def capture(cls, iface):
        raw = command(["tc", "qdisc", "show", "dev", iface]).stdout
        classes = command(["tc", "-d", "class", "show", "dev", iface]).stdout
        entries = []
        for line in raw.splitlines():
            words = line.split()
            if len(words) < 4 or words[0] != "qdisc":
                raise TaskError("无法解析当前队列，未修改网络参数")
            kind, handle = words[1:3]
            if kind in ("ingress", "clsact"):
                continue  # 独立的入向队列不会随根队列删除。
            if kind not in cls.OPTIONS:
                raise TaskError("暂不支持原样恢复 {} 队列，未修改网络参数".format(kind))
            if words[3] == "root":
                parent, rest = "root", words[4:]
            elif words[3] == "parent" and len(words) > 4:
                parent, rest = words[4], words[5:]
            else:
                raise TaskError("无法识别队列层级，未修改网络参数")
            entries.append({"kind": kind, "handle": handle, "parent": parent, "options": cls.parse_options(kind, rest)})
        roots = [entry for entry in entries if entry["parent"] == "root"]
        if len(roots) != 1:
            raise TaskError("无法确定唯一出口根队列，未修改网络参数")
        root = roots[0]
        if root["kind"] == "pfifo_fast":
            expected = ["bands", "3", "priomap", "1", "2", "2", "2", "1", "2", "0", "0", "1", "1", "1", "1", "1", "1", "1", "1"]
            if root["options"] != expected:
                raise TaskError("pfifo_fast 不是可重建的内核默认参数，未修改网络参数")
        class_entries, rate = [], None
        for line in classes.splitlines():
            words = line.split()
            if root["kind"] == "mq" and len(words) > 1 and words[1] == "mq":
                continue
            if len(words) < 6 or words[:2] != ["class", "htb"]:
                raise TaskError("只支持可完整恢复的单层 HTB 整形，未修改网络参数")
            handle = words[2]
            index = 3
            if words[index] == "root":
                parent = root["handle"]
                index += 1
            elif words[index] == "parent":
                parent = words[index + 1]
                index += 2
            else:
                raise TaskError("无法识别 HTB class 层级")
            opts = []
            seen = {}
            while index < len(words):
                key = words[index]
                index += 1
                if key == "leaf":
                    index += 1
                    continue
                if key == "level" and index < len(words) and words[index] == "0":
                    index += 1
                    continue
                if key not in cls.CLASS_OPTIONS or index >= len(words):
                    raise TaskError("无法可靠恢复 HTB 参数: " + key)
                value = words[index]
                index += 1
                if key == "mpu":
                    value = cls.size_bytes(value)
                if key in seen:
                    if seen[key] != value:
                        raise TaskError("HTB 同名参数值不同，无法保证原样恢复: " + key)
                    continue
                seen[key] = value
                # burst 的 /cell 是 tc 可接受的形式，完整保留。
                opts.extend([key, value])
            class_entries.append({"handle": handle, "parent": parent, "options": opts})
        if root["kind"] == "htb":
            leaves = [entry for entry in entries if entry["parent"] != "root"]
            if len(class_entries) != 1 or len(leaves) != 1 or leaves[0]["kind"] != "fq":
                raise TaskError("当前 HTB 不是单级整形加 fq 叶子，未修改网络参数")
            item = class_entries[0]
            if item["parent"] != root["handle"] or leaves[0]["parent"] != item["handle"]:
                raise TaskError("HTB 层级不符，未修改网络参数")
            default = root["options"][root["options"].index("default") + 1] if "default" in root["options"] else "0"
            if int(default, 16) != int(item["handle"].split(":")[1], 16):
                raise TaskError("HTB 默认流量未进入唯一整形 class，不能当作全局整形")
            opts = item["options"]
            if "rate" not in opts or "ceil" not in opts:
                raise TaskError("HTB 缺少 rate/ceil 参数")
            rate_text = opts[opts.index("rate") + 1]
            ceil_text = opts[opts.index("ceil") + 1]
            rate = cls.rate_mbps(rate_text)
            if rate != cls.rate_mbps(ceil_text) or not float(rate).is_integer():
                raise TaskError("当前 HTB 的 rate/ceil 不相同或不是整数 Mbps，未修改网络参数")
            rate = int(rate)
        elif class_entries:
            raise TaskError("检测到不支持的 class 结构，未修改网络参数")
        parents = {entry["handle"] for entry in entries} | {entry["handle"] for entry in class_entries}
        for parent in parents:
            if parent == "0:":
                continue
            filters = command(["tc", "filter", "show", "dev", iface, "parent", parent]).stdout.strip()
            if filters:
                raise TaskError("出口队列包含自定义 filter，无法保证原样恢复，未修改网络参数")
        limited_fq = any(entry["kind"] == "fq" and "maxrate" in entry["options"] and entry["options"][entry["options"].index("maxrate") + 1] != "unlimited" for entry in entries)
        return {"iface": iface, "qdiscs": entries, "classes": class_entries, "rate": rate, "limited_fq": limited_fq, "raw_qdisc": raw, "raw_class": classes}

    @staticmethod
    def rate_mbps(value):
        match = re.fullmatch(r"([0-9.]+)([KMG]?)bit", value)
        if not match:
            raise TaskError("无法解析整形速率: " + value)
        return float(match[1]) * {"": 0.000001, "K": 0.001, "M": 1, "G": 1000}[match[2]]

    @staticmethod
    def restore(state):
        iface, entries = state["iface"], state["qdiscs"]
        root = next(entry for entry in entries if entry["parent"] == "root")
        command(["tc", "qdisc", "del", "dev", iface, "root"], check=False)
        current = command(["tc", "qdisc", "show", "dev", iface]).stdout
        old_major = root["handle"].split(":")[0]
        new_major = old_major
        if root["kind"] == "noqueue":
            if not re.search(r"qdisc noqueue .* root", current):
                raise TaskError("无法恢复 noqueue 根队列")
            return
        # 内核自动生成的 mq 0: 无法寻址叶子，使用可寻址句柄重建同样的队列。
        if root["kind"] == "mq" and old_major == "0":
            used = {int(entry["handle"].split(":")[0] or "0", 16) for entry in entries}
            new_major = next(format(value, "x") for value in range(1, 65536) if value not in used)
        root_handle = new_major + ":"
        if root["kind"] == "pfifo_fast":
            if "qdisc pfifo_fast 0: root" in current and old_major == "0":
                return
            args = ["tc", "qdisc", "replace", "dev", iface, "root"]
            if root_handle != "0:":
                args += ["handle", root_handle]
            command(args + ["pfifo_fast"])
            return
        if old_major == "0" and root["kind"] != "mq" and "qdisc {} 0: root".format(root["kind"]) in current:
            command(["tc", "qdisc", "change", "dev", iface, "root", root["kind"]] + root["options"])
        else:
            args = ["tc", "qdisc", "replace", "dev", iface, "root"]
            if root_handle != "0:":
                args += ["handle", root_handle]
            command(args + [root["kind"]] + root["options"])
        for item in state["classes"]:
            command(["tc", "class", "replace", "dev", iface, "parent", item["parent"], "classid", item["handle"], "htb"] + item["options"])
        for item in entries:
            if item["parent"] == "root":
                continue
            parent = item["parent"]
            if new_major != old_major and parent.split(":")[0] in (old_major, ""):
                parent = new_major + ":" + parent.split(":")[1]
            args = ["tc", "qdisc", "replace", "dev", iface, "parent", parent]
            if item["handle"] != "0:":
                args += ["handle", item["handle"]]
            command(args + [item["kind"]] + item["options"])


class Snapshot:
    @staticmethod
    def capture(iface, keys, state_dir):
        files = {}
        for name in CONFIG_FILES + (str(Path(state_dir) / "initcwnd.owned"),):
            path = Path(name)
            if path.is_symlink():
                if "multi-user.target.wants" not in name:
                    raise TaskError("配置文件是自定义符号链接，无法保证恢复: " + name)
                files[name] = {"link": os.readlink(name)}
            elif path.exists():
                stat = path.stat()
                files[name] = {"data": base64.b64encode(path.read_bytes()).decode("ascii"), "mode": stat.st_mode & 0o7777, "uid": stat.st_uid, "gid": stat.st_gid}
            else:
                files[name] = None
        values = {}
        for key in keys:
            result = command(["sysctl", "-n", key], check=False)
            if result.returncode == 0:
                values[key] = result.stdout.strip()
        return {
            "files": files, "sysctl": values, "queue": QueueState.capture(iface),
            "routes": {family: command(["ip", family, "route", "show", "default"]).stdout.splitlines() for family in ("-4", "-6")},
            "captured_at": time.time(),
            "service_active": command(["systemctl", "is-active", "tcpfit-qdisc.service"], check=False).stdout.strip() == "active",
        }

    @staticmethod
    def restore_files(files):
        for name, saved in files.items():
            path = Path(name)
            if saved is None:
                if path.exists() or path.is_symlink():
                    path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink():
                path.unlink()
            if "link" in saved:
                if path.exists():
                    path.unlink()
                path.symlink_to(saved["link"])
            else:
                temp = path.with_name(path.name + ".return-restore")
                with open(str(temp), "wb") as output:
                    output.write(base64.b64decode(saved["data"]))
                os.chmod(str(temp), saved["mode"])
                os.chown(str(temp), saved["uid"], saved["gid"])
                os.replace(str(temp), str(path))

    @staticmethod
    def route_identity(line):
        parts = line.split()
        return tuple(parts[parts.index(key) + 1] if key in parts else "" for key in ("via", "dev", "metric", "table"))

    @staticmethod
    def route_config(line):
        # RA 等自动路由的剩余寿命不是配置变化，不能因此反复回写路由。
        return re.sub(r"\s+expires\s+\S+", "", line).split()

    @staticmethod
    def route_restore_args(line, captured_at=None):
        args = shlex.split(line)
        if "expires" in args:
            index = args.index("expires")
            value = args[index + 1]
            if not re.fullmatch(r"[0-9]+(?:sec)?", value):
                raise TaskError("无法恢复路由有效期: " + value)
            seconds = int(value[:-3] if value.endswith("sec") else value)
            elapsed = max(0, time.time() - captured_at) if captured_at is not None else 0
            args[index + 1] = str(max(1, int(seconds - elapsed)))
        return args

    @classmethod
    def restore(cls, state):
        failures = []
        command(["systemctl", "stop", "tcpfit-qdisc.service"], check=False)
        for key, value in state["sysctl"].items():
            try:
                command(["sysctl", "-qw", key + "=" + value])
            except TaskError as error:
                failures.append(str(error))
        try:
            cls.restore_files(state["files"])
            command(["systemctl", "daemon-reload"])
            if state["service_active"]:
                command(["systemctl", "start", "tcpfit-qdisc.service"])
        except (TaskError, OSError) as error:
            failures.append(str(error))
        for family, original in state["routes"].items():
            current = command(["ip", family, "route", "show", "default"]).stdout.splitlines()
            if sorted(cls.route_config(line) for line in current) == sorted(cls.route_config(line) for line in original):
                continue
            try:
                for line in original:
                    command(["ip", family, "route", "replace"] + cls.route_restore_args(line, state.get("captured_at")))
                identities = {cls.route_identity(line) for line in original}
                for line in command(["ip", family, "route", "show", "default"]).stdout.splitlines():
                    if cls.route_identity(line) not in identities:
                        command(["ip", family, "route", "del"] + cls.route_config(line))
            except TaskError as error:
                failures.append(str(error))
        try:
            QueueState.restore(state["queue"])
        except TaskError as error:
            failures.append(str(error))
        if failures:
            raise TaskError("恢复未完成: " + "; ".join(failures))


WORKER = r'''
source "$1"
shift
LOCK_HELD=1
WIZARD=1
ARCH_INCLUDE_SWEEP=0
self_install(){ :; }
migrate_legacy(){ :; }
action="$1"; shift
case "$action" in
  profile) printf '%s\n' "$(detect_iface)" "$DEFAULT_RTT" "$VDUR" "$VERIFY_GOOD_PCT" "$VERIFY_ACCEPT_PCT" ;;
  keys) printf '%s\n' $TUNED_KEYS ;;
  snapshot) take_snapshot ;;
  probe) measure_bandwidth return-path "$VDUR" ;;
  verify) verify_measure return-path ;;
  tune) cmd_tune "$@" ;;
  fq) qdisc_remove_root "$1" && qdisc_set_fq "$1" ;;
  test-shape) apply_test_shaper "$1" "$2" ;;
  shape) cmd_shape --rate "$1" ;;
  margin) calc_margin "$1" ;;
  spike) retrans_is_spike "$1" "$2" ;;
  band) retrans_band "$1" ;;
  archive)
    name="$1"; ARCH_ROLE="$2"; ARCH_BW="$3"; ARCH_RTT="$4"; ARCH_PEER="$5"
    ARCH_MODE=return; ARCH_RUN="$6"
    ARCH_RETURN_SNAPSHOT="$7"
    archive_save "$name" ;;
  *) exit 1 ;;
esac
'''


class Worker:
    def __init__(self, script, run_dir=None, lock_fd=None, check=None):
        self.script, self.run_dir, self.lock_fd, self.check = Path(script).as_posix(), run_dir, lock_fd, check
        self.stage = "probe"
        self.children_file = Path(run_dir) / "worker.json" if run_dir else None

    def run(self, action, *args, quiet=False, allow_failure=False):
        env = dict(os.environ, TCPFIT_NO_TELEMETRY="1")
        if self.run_dir:
            env.update(TCPFIT_RETURN_RUN=str(self.run_dir), TCPFIT_RETURN_HELPER=str(Path(__file__).resolve()), TCPFIT_RETURN_STAGE=self.stage)
        process = subprocess.Popen(
            [shutil.which("bash") or "bash", "-c", WORKER, "tcpfit-return-worker", self.script, action] + [str(arg) for arg in args],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, encoding="utf-8", errors="replace",
            start_new_session=True, pass_fds=(() if self.lock_fd is None else (self.lock_fd,)),
        )
        entry = {"pid": process.pid, "stamp": process_stamp(process.pid)}
        if self.children_file:
            atomic_json(self.children_file, entry)
        try:
            while True:
                try:
                    output = process.communicate(timeout=0.5)[0]
                    break
                except subprocess.TimeoutExpired:
                    if self.check:
                        self.check()
        except BaseException:
            stop_process(entry)
            process.communicate()
            raise
        finally:
            if self.children_file and self.children_file.exists():
                self.children_file.unlink()
        if not quiet and output.strip():
            print(output.rstrip(), flush=True)
        if process.returncode and not allow_failure:
            raise TaskError("公共调优步骤 {} 失败: {}".format(action, output.strip()))
        return output.strip(), process.returncode


class Firewall:
    def __init__(self, run_dir, family, control_port, iperf_port, task_id):
        self.path = Path(run_dir) / "firewall.json"
        self.state = {
            "binary": "iptables" if family == 4 else "ip6tables", "family": family,
            "chain": "TFRET_" + task_id[:12], "control": control_port, "iperf": iperf_port,
            "tag": "tcpfit-return:" + task_id, "created": False, "native_chains": [],
            "table": "tcpfit_return_" + task_id, "backend": "none", "manager": None,
        }

    @staticmethod
    def select(family):
        binary = "iptables" if family == 4 else "ip6tables"
        available = shutil.which(binary)
        native = shutil.which("nft")
        if shutil.which("ufw"):
            status = command(["ufw", "status"], check=False)
            if status.returncode:
                raise TaskError("已有 UFW 状态读取失败: " + status.stderr.strip())
            if re.search(r"^Status: active\s*$", status.stdout, re.MULTILINE):
                # UFW 使用现有内核防火墙；临时链放在其分发规则之前，不改 UFW 配置。
                if available:
                    return {"backend": "iptables", "binary": binary, "manager": "ufw"}
                if native:
                    return {"backend": "nft", "manager": "ufw"}
                raise TaskError("UFW 已启用，但找不到可用的底层防火墙工具；本程序不安装这些工具")
        if native:
            # 已有 legacy 规则无法通过 nft 查看；继续用原工具并兼容原生 nft 链。
            if available and "nf_tables" not in command([binary, "--version"]).stdout:
                rules = command([binary, "-S"]).stdout
                if any(line.startswith("-A ") or re.match(r"-P \S+ (DROP|REJECT)$", line) for line in rules.splitlines()):
                    return {"backend": "iptables", "binary": binary, "manager": None}
            return {"backend": "nft", "manager": None}
        if available:
            return {"backend": "iptables", "binary": binary, "manager": None}
        return {"backend": "none", "manager": None}

    def description(self):
        backend = self.state["backend"]
        if backend == "none":
            return "无可用防火墙工具，跳过测速端口的来源 IP 限制，不安装工具"
        tool = "nftables" if backend == "nft" else self.state["binary"]
        return "UFW（复用现有 {} 添加临时规则）".format(tool) if self.state["manager"] == "ufw" else tool + "（临时规则）"

    def save(self):
        atomic_json(self.path, self.state)

    def iptables(self, *args, check=True):
        return command([self.state["binary"], "-w", "5"] + list(args), check=check)

    @staticmethod
    def nft_rules():
        if not shutil.which("nft"):
            return []
        result = command(["nft", "-j", "list", "ruleset"], check=False)
        if result.returncode:
            raise TaskError("已有 nftables 规则读取失败: " + result.stderr.strip())
        try:
            rules = json.loads(result.stdout)["nftables"]
            if not isinstance(rules, list):
                raise ValueError("规则列表无效")
            return rules
        except (ValueError, KeyError, TypeError) as error:
            raise TaskError("无法解析已有 nftables 规则: " + str(error))

    def native_allow(self, peer=None):
        # 某些主机同时使用 iptables 和原生 nft input 链；后者的 drop 策略仍会生效。
        # 只在已有 input 链插入本任务所需的精确允许规则，按唯一注释清理。
        family = self.state["family"]
        proto = "ip" if family == 4 else "ip6"
        port = self.state["iperf"] if peer else self.state["control"]
        tag = self.state["tag"] + (":peer" if peer else ":control")
        expressions = []
        if peer:
            expressions.append({"match": {"op": "==", "left": {"payload": {"protocol": proto, "field": "saddr"}}, "right": peer}})
        expressions.extend([{"match": {"op": "==", "left": {"payload": {"protocol": "tcp", "field": "dport"}}, "right": port}},
                            {"counter": None}, {"accept": None}])
        for chain in self.state["native_chains"]:
            # ip/ip6 表已经限定协议族，只有 inet 表接受 nfproto 条件。
            family_match = [{"match": {"op": "==", "left": {"meta": {"key": "nfproto"}},
                                        "right": "ipv4" if family == 4 else "ipv6"}}] if chain["family"] == "inet" else []
            rule = {"family": chain["family"], "table": chain["table"], "chain": chain["name"],
                    "expr": family_match + expressions, "comment": tag}
            # JSON 接口直接传递已有名称，避免把表名、链名当成 nft 脚本语法。
            command(["nft", "-j", "-f", "-"], input_data=json.dumps({"nftables": [{"insert": {"rule": rule}}]}))

    def setup(self):
        self.state.update(self.select(self.state["family"]))
        self.save()
        log("端口规则: " + self.description())
        if self.state["backend"] == "none":
            return
        binary = self.state["binary"]
        iptables_nft = self.state["backend"] == "iptables" and "nf_tables" in command([binary, "--version"]).stdout
        rules = self.nft_rules()
        for entry in rules:
            chain = entry.get("chain", {})
            if chain.get("hook") != "input" or chain.get("family") not in ("inet", "ip" if self.state["family"] == 4 else "ip6"):
                continue
            if iptables_nft and chain.get("table") == "filter" and chain.get("name") == "INPUT" and chain.get("family") != "inet":
                continue
            self.state["native_chains"].append(chain)
        self.save()
        if self.state["backend"] == "nft":
            table = self.state["table"]
            if any(row.get("table", {}).get("family") == "inet" and row["table"].get("name") == table for row in rules):
                raise TaskError("临时 nftables 表名称已存在，未更改现有表")
            self.state["created"] = True
            self.save()
            # 独立表只拦截本次测速端口；其他流量继续经过原有规则。
            command(["nft", "-f", "-"], input_data=(
                "add table inet {table}\n"
                "add chain inet {table} input {{ type filter hook input priority -10; policy accept; }}\n"
                "add rule inet {table} input meta nfproto ipv{family} tcp dport {port} drop\n"
            ).format(table=table, family=self.state["family"], port=self.state["iperf"]))
            self.native_allow()
            return
        chain = self.state["chain"]
        if self.iptables("-S", chain, check=False).returncode == 0:
            raise TaskError("临时防火墙链名称已存在，未更改现有链")
        self.state["created"] = True
        self.save()
        self.iptables("-N", chain)
        self.iptables("-A", chain, "-p", "tcp", "--dport", str(self.state["control"]), "-j", "ACCEPT")
        self.iptables("-A", chain, "-j", "DROP")
        self.iptables("-I", "INPUT", "1", "-p", "tcp", "-m", "multiport", "--dports", "{},{}".format(self.state["control"], self.state["iperf"]), "-j", chain)
        self.native_allow()

    def pair(self, peer):
        peer = str(ipaddress.ip_address(peer))
        if self.state["backend"] == "nft":
            command(["nft", "-f", "-"], input_data=(
                "insert rule inet {} input {} saddr {} tcp dport {} accept\n".format(
                    self.state["table"], "ip" if self.state["family"] == 4 else "ip6", peer, self.state["iperf"])))
        elif self.state["backend"] == "iptables":
            self.iptables("-I", self.state["chain"], "1", "-s", peer, "-p", "tcp", "--dport", str(self.state["iperf"]), "-j", "ACCEPT")
        if self.state["backend"] != "none":
            self.native_allow(peer)
        self.state["peer"] = peer
        self.save()

    @classmethod
    def cleanup(cls, path):
        path = Path(path)
        if not path.exists():
            return
        state = read_json(path)
        # 旧版恢复记录没有 backend 字段，仍按原来的 iptables 规则清理。
        backend = state.get("backend", "iptables")
        if backend == "none":
            path.unlink()
            return
        failures = []
        rules = cls.nft_rules()
        for entry in rules:
            rule = entry.get("rule", {})
            if rule.get("comment") not in (state["tag"] + ":peer", state["tag"] + ":control"):
                continue
            try:
                identity = {key: rule[key] for key in ("family", "table", "chain", "handle")}
                command(["nft", "-j", "-f", "-"], input_data=json.dumps({"nftables": [{"delete": {"rule": identity}}]}))
            except TaskError as error:
                failures.append(str(error))
        if state["created"] and backend == "nft":
            try:
                if any(row.get("table", {}).get("family") == "inet" and row["table"].get("name") == state["table"] for row in rules):
                    command(["nft", "delete", "table", "inet", state["table"]])
            except TaskError as error:
                failures.append(str(error))
        elif state["created"]:
            prefix = [state["binary"], "-w", "5"]
            jump = ["INPUT", "-p", "tcp", "-m", "multiport", "--dports", "{},{}".format(state["control"], state["iperf"]), "-j", state["chain"]]
            try:
                if command(prefix + ["-C"] + jump, check=False).returncode == 0:
                    command(prefix + ["-D"] + jump)
                if command(prefix + ["-S", state["chain"]], check=False).returncode == 0:
                    command(prefix + ["-F", state["chain"]])
                    command(prefix + ["-X", state["chain"]])
            except TaskError as error:
                failures.append(str(error))
        if failures:
            raise TaskError("临时端口规则清理失败: " + "; ".join(failures))
        path.unlink()


def clean_raw(document):
    # iperf3 自己的 cookie 只用于本次连接识别，无需写入长期记录。
    if isinstance(document, dict):
        return {key: clean_raw(value) for key, value in document.items() if key != "cookie"}
    if isinstance(document, list):
        return [clean_raw(value) for value in document]
    return document


class Coordinator:
    def __init__(self, args, run_dir, record_dir, firewall):
        self.args, self.run_dir, self.record_dir, self.firewall = args, Path(run_dir), Path(record_dir), firewall
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(24)
        self.session = None
        self.peer = None
        self.expires = time.monotonic() + args.token_ttl
        self.started = time.monotonic()
        self.last_seen = None
        self.error = None
        self.finished = None
        self.done_ack = threading.Event()
        self.paired = threading.Event()
        self.active = None
        self.results = []
        self.closed = False
        self.httpd = None
        self.owner_parent = os.getppid()
        self.parent_stamp = process_stamp(self.owner_parent)

    def fail(self, reason):
        with self.lock:
            if self.closed:
                return
            if self.error is None:
                self.error = str(reason)
                atomic_json(self.run_dir / "error.json", {"error": self.error})
                log(self.error)

    def check(self):
        if self.error:
            raise TaskError(self.error)
        if process_stamp(self.owner_parent) != self.parent_stamp:
            raise TaskError("调优入口进程已退出，正在恢复配置")
        now = time.monotonic()
        if now - self.started > TASK_TIMEOUT:
            raise TaskError("任务超过 30 分钟，已停止测速")
        if self.peer and now - self.last_seen > HEARTBEAT_TIMEOUT:
            raise TaskError("测速端连接中断：45 秒内未收到心跳")
        if not self.peer and now > self.expires:
            raise TaskError("配对 token 已过期，未修改网络参数")

    def authenticate(self, authorization, address):
        with self.lock:
            good = self.session and hmac.compare_digest(authorization.encode("utf-8"), ("Bearer " + self.session).encode("ascii")) and address == self.peer
            if good:
                self.last_seen = time.monotonic()
            return bool(good)

    def pair(self, authorization, address, version):
        with self.lock:
            if self.closed or self.finished is not None:
                return 410, "本任务已经结束，临时凭据已撤销"
            if self.peer:
                return 409, "本任务已绑定一个测速端，不能重复接入或更换测速端"
            if time.monotonic() > self.expires:
                return 410, "临时 token 已过期，请重新启动回国调优"
            if not self.token or not hmac.compare_digest(authorization.encode("utf-8"), ("Pair " + self.token).encode("ascii")):
                log("接入认证失败：临时 token 不正确")
                return 403, "认证失败：临时 token 不正确"
            if version != VERSION:
                return 409, "测速端脚本版本与调优端不一致，请重新复制接入命令"
            try:
                self.firewall.pair(address)
            except (TaskError, OSError) as error:
                self.fail("配对后的端口规则应用失败: " + str(error))
                self.token = None
                return 500, self.error
            self.peer, self.session, self.token = address, secrets.token_hex(24), None
            self.last_seen = time.monotonic()
            self.paired.set()
            log("测速端已配对，后续自动执行")
            return 200, "OK " + self.session

    def next_job(self):
        with self.lock:
            if self.closed:
                raise TaskError("任务已关闭，不能再启动测速")
            if self.finished is not None:
                self.done_ack.set()
                return "DONE " + self.finished
            if self.error:
                return "FAIL " + self.error
            if self.active:
                return "WAIT"
            request_file = self.run_dir / "request.json"
            if not request_file.exists():
                return "WAIT"
            job = read_json(request_file)
            if not re.fullmatch(r"[a-f0-9]{16}", job.get("id", "")) or job.get("streams") not in (1, 4) or not 1 <= job.get("duration", 0) <= 600:
                raise TaskError("本地测量请求无效")
            if (self.run_dir / ("result-" + job["id"] + ".json")).exists():
                return "WAIT"
            raw_path = self.run_dir / (job["id"] + ".server.json")
            output = open(str(raw_path), "w", encoding="utf-8")
            process = subprocess.Popen(
                ["iperf3", "-{}".format(self.args.family), "-s", "-1", "-J", "-p", str(self.args.iperf_port)],
                stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
            )
            output.close()
            entry = {"pid": process.pid, "stamp": process_stamp(process.pid)}
            atomic_json(self.run_dir / "iperf.json", entry)
            job.update(process=process, process_entry=entry, raw_path=raw_path, latency={})
            self.active = job
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise TaskError("临时 iperf3 服务启动失败: " + raw_path.read_text(encoding="utf-8")[:1000])
                result = command(["ss", "-H", "-ltn", "sport", "=", str(self.args.iperf_port)], check=False)
                if result.returncode == 0 and result.stdout.strip():
                    break
                time.sleep(0.1)
            else:
                raise TaskError("临时 iperf3 服务未监听测速端口")
            log("{}：{} 秒 × {} 连接，服务器发送、家宽接收".format(job["stage"], job["duration"], job["streams"]))
            return "RUN {} {} {}".format(job["id"], job["duration"], job["streams"])

    def job_for(self, job_id):
        if not self.active or self.active["id"] != job_id:
            raise TaskError("结果不属于当前测试，或本轮结果已经提交")
        return self.active

    def save_latency(self, job_id, phase, raw):
        with self.lock:
            job = self.job_for(job_id)
            if phase in job["latency"]:
                raise TaskError("本轮延迟数据已经提交")
            job["latency"][phase] = latency_summary(raw)

    def result(self, job_id, raw):
        with self.lock:
            job = self.job_for(job_id)
            try:
                rc = job["process"].wait(timeout=10)
            except subprocess.TimeoutExpired:
                raise TaskError("调优端 iperf3 未正常结束")
            if rc:
                raise TaskError("调优端 iperf3 执行失败（退出码 {}）".format(rc))
            try:
                client, server = json.loads(raw), read_json(job["raw_path"])
            except (ValueError, OSError):
                raise TaskError("iperf3 未返回完整 JSON 结果")
            measured = parse_measurement(client, server, job["streams"], job["duration"])
            if set(job["latency"]) != {"idle", "loaded"}:
                raise TaskError("测速端未回报空载和满载延迟采集结果")
            measured.update(id=job_id, stage=job["stage"], timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), latency=job["latency"])
            record = dict(measured, raw_client=clean_raw(client), raw_server=clean_raw(server))
            atomic_json(self.record_dir / "measurements" / (job_id + ".json"), record)
            self.results.append(measured)
            atomic_json(self.run_dir / ("result-" + job_id + ".json"), measured)
            job["raw_path"].unlink()
            (self.run_dir / "iperf.json").unlink()
            self.active = None
            log("有效结果：{:.2f} Mbps，服务器重传 {} 次".format(measured["receiver_mbps"], measured["retransmits"]))

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.token, self.session = None, None
            active = self.active
        try:
            if active:
                stop_process(active["process_entry"])
                active["process"].wait(timeout=5)
        finally:
            if self.httpd:
                self.httpd.shutdown()
                self.httpd.server_close()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "tcpfit-return"
    sys_version = ""

    def log_message(self, *args):
        pass  # HTTP 访问日志不记录认证头、临时 token 或会话凭据。

    def reply(self, status, value, content_type="text/plain; charset=utf-8"):
        data = value if isinstance(value, bytes) else (value + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def handle_request(self, post):
        coordinator = self.server.coordinator
        address = str(ipaddress.ip_address(self.client_address[0]))
        authorization = self.headers.get("Authorization", "")
        authenticated = coordinator.authenticate(authorization, address)
        try:
            if not post and self.path == "/join.sh":
                return self.reply(200, Path(coordinator.args.client_script).read_bytes(), "text/x-shellscript")
            if post and self.path == "/pair":
                status, response = coordinator.pair(authorization, address, self.headers.get("X-Tcpfit-Version", ""))
                return self.reply(status, response)
            if not authenticated:
                return self.reply(403, "会话认证失败或来源 IP 已改变")
            if not post:
                if self.path == "/next":
                    return self.reply(200, coordinator.next_job())
                if self.path in ("/ping", "/heartbeat"):
                    return self.reply(200, "OK")
                return self.reply(404, "未知请求")
            length = self.headers.get("Content-Length", "")
            if not length.isdigit() or not 0 <= int(length) <= MAX_BODY:
                raise TaskError("请求体大小无效")
            raw = self.rfile.read(int(length))
            if len(raw) != int(length):
                raise TaskError("连接中断，未收到完整测量数据")
            raw = raw.decode("utf-8")
            if self.path == "/error":
                coordinator.fail("测速端失败: " + raw[:1000])
                return self.reply(200, "已收到失败报告，调优端将停止任务并恢复配置")
            match = re.fullmatch(r"/latency/([a-f0-9]{16})/(idle|loaded)", self.path)
            if match:
                coordinator.save_latency(match[1], match[2], raw)
                return self.reply(200, "OK")
            match = re.fullmatch(r"/result/([a-f0-9]{16})", self.path)
            if match:
                coordinator.result(match[1], raw)
                return self.reply(200, "OK")
            self.reply(404, "未知请求")
        except (TaskError, ValueError, OSError, subprocess.SubprocessError) as error:
            if authenticated:
                coordinator.fail(str(error))
            try:
                self.reply(400, str(error))
            except OSError:
                pass

    def do_GET(self):
        self.handle_request(False)

    def do_POST(self):
        self.handle_request(True)


class ControlServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        super().server_bind()

    def process_request_thread(self, request, address):
        try:
            request.settimeout(15)
            super().process_request_thread(request, address)
        except OSError:
            request.close()


def start_http(coordinator):
    family = socket.AF_INET if coordinator.args.family == 4 else socket.AF_INET6
    class Server(ControlServer):
        address_family = family
    server = Server(("0.0.0.0" if family == socket.AF_INET else "::", coordinator.args.control_port), Handler)
    server.coordinator = coordinator
    coordinator.httpd = server
    threading.Thread(target=server.serve_forever, daemon=True).start()


def request_measurement(args):
    run_dir = Path(args.run_dir)
    job_id = secrets.token_hex(8)
    result_path = run_dir / ("result-" + job_id + ".json")
    request_path = run_dir / "request.json"
    if request_path.exists():
        raise TaskError("已有一次测速正在进行，请勿重复请求")
    atomic_json(request_path, {"id": job_id, "streams": args.streams, "duration": args.duration, "stage": args.stage})
    deadline = time.monotonic() + args.duration + 90
    while time.monotonic() < deadline:
        if (run_dir / "error.json").exists():
            raise TaskError(read_json(run_dir / "error.json")["error"])
        if result_path.exists():
            result = read_json(result_path)
            print("{:.6f} {} {:.6f}".format(result["sender_mbps"], result["retransmits"], result["receiver_mbps"]))
            request_path.unlink()
            result_path.unlink()
            return
        time.sleep(0.2)
    raise TaskError("等待家宽测量结果超时")


def representative(results, streams):
    group = sorted((row for row in results if row["streams"] == streams), key=lambda row: row["receiver_mbps"])
    if not group:
        raise TaskError("缺少 {} 连接有效结果".format(streams))
    return group[len(group) // 2]


def median_metric(results, streams, key):
    return statistics.median(row[key] for row in results if row["streams"] == streams)


def retrans_acceptable(worker, before, after):
    reasons = []
    for streams in (1, 4):
        baseline = median_metric(before, streams, "estimated_retrans_pct")
        current = median_metric(after, streams, "estimated_retrans_pct")
        previous_band = int(worker.run("band", baseline, quiet=True)[0])
        current_band = int(worker.run("band", current, quiet=True)[0])
        hits = sum(worker.run("spike", row["estimated_retrans_pct"], baseline, quiet=True, allow_failure=True)[1] == 0 for row in after if row["streams"] == streams)
        if current_band > previous_band:
            reasons.append("{} 连接的估算重传比分档变差".format(streams))
        if hits >= 2:
            reasons.append("{} 连接至少两次出现原版判据定义的重传跳变".format(streams))
    return reasons


def base_decision(worker, before, after):
    reasons = retrans_acceptable(worker, before, after)
    for streams in (1, 4):
        if median_metric(after, streams, "receiver_mbps") < median_metric(before, streams, "receiver_mbps"):
            reasons.append("{} 连接的三次吞吐中位数下降".format(streams))
    return not reasons, reasons or ["单连接和四连接吞吐中位数均未下降，重传分档未变差且没有重复跳变"]


def shape_candidate(worker, old_rate, results):
    if old_rate is None:
        return None, "原本没有全局整形，本次不创建"
    samples = [row["receiver_mbps"] for row in results if row["streams"] == 4]
    if len(samples) < 3 or min(samples) <= old_rate:
        return None, "三次四连接实测未全部超过旧上限，恢复原整形"
    stable = int(min(samples))
    margin = int(worker.run("margin", stable, quiet=True)[0])
    rate = stable - margin
    if rate <= old_rate:
        return None, "按原版安全余量计算后未高于旧值，恢复原整形"
    if rate > 100000:
        return None, "候选值超过原版整形支持范围，恢复原整形"
    return rate, "三次四连接实测均超过旧上限；取最低吞吐减去原版安全余量，只验证 {} Mbps".format(rate)


def shape_decision(worker, reference, measured, old_rate, candidate, accept_pct):
    reasons = retrans_acceptable(worker, reference, measured)
    if median_metric(measured, 4, "receiver_mbps") < candidate * accept_pct / 100:
        reasons.append("四连接吞吐未达到原版可接受的整形值 {}%".format(accept_pct))
    if min(row["receiver_mbps"] for row in measured if row["streams"] == 4) <= old_rate:
        reasons.append("提高整形后没有稳定超过旧上限")
    return not reasons, reasons


def clear_pending(journal):
    path = Path(journal["state_dir"]) / "return-pending.json"
    if path.exists() and read_json(path).get("record_dir") == journal["record_dir"]:
        path.unlink()


def remove_credentials(run_dir):
    """兼容旧版本恢复记录，清理旧任务可能留下的临时证书。"""
    for name in ("server.key", "server.crt"):
        path = Path(run_dir) / name
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def recover(record_dir):
    record_dir = Path(record_dir)
    journal = read_json(record_dir / "transaction.json")
    if process_stamp(journal["owner_pid"]) == journal["owner_stamp"]:
        raise TaskError("任务仍在运行，不能执行恢复或抢占当前任务")
    run_dir = Path(journal["run_dir"])
    failures = []
    for name in ("worker.json", "iperf.json"):
        path = run_dir / name
        if path.exists():
            stop_process(read_json(path))
    try:
        Firewall.cleanup(record_dir / "firewall.json")
        Firewall.cleanup(run_dir / "firewall.json")  # 兼容开发阶段的恢复记录。
    except (TaskError, OSError) as error:
        failures.append(str(error))
    if journal["dirty"] and not journal["committed"]:
        try:
            Snapshot.restore(read_json(record_dir / "before.json"))
            journal["restored"] = True
        except (TaskError, OSError) as error:
            failures.append(str(error))
    remove_credentials(run_dir)
    if failures:
        journal["recovery_error"] = "; ".join(failures)
        atomic_json(record_dir / "transaction.json", journal)
        atomic_json(Path(journal["state_dir"]) / "return-pending.json", {"record_dir": str(record_dir)})
        raise TaskError(journal["recovery_error"])
    journal["done"] = True
    atomic_json(record_dir / "transaction.json", journal)
    result_file = record_dir / "result.json"
    result = read_json(result_file) if result_file.exists() else {}
    if not journal["committed"]:
        if result.get("status") not in ("failed", "aborted"):
            result.update(status="aborted", error="任务进程异常退出")
        result.update(recovered=True, recovery_message="已恢复任务开始前的配置")
    result["cleanup_complete"] = True
    atomic_json(result_file, result)
    clear_pending(journal)
    if run_dir.is_dir():
        shutil.rmtree(str(run_dir))


def guard(record_dir):
    path = Path(record_dir) / "transaction.json"
    initial = read_json(path)
    while process_stamp(initial["owner_pid"]) == initial["owner_stamp"]:
        if read_json(path).get("done"):
            return
        time.sleep(0.25)
    recover(record_dir)


def join_command(args, token):
    host = "[{}]".format(args.server) if args.family == 6 else args.server
    direct = shlex.quote("http://{}:{}/join.sh".format(host, args.control_port))
    values = [args.server, str(args.control_port), str(args.iperf_port), token]
    # 两种下载工具都直接获取调优端提供的同版本脚本，无需发布标签或证书。
    return "(curl -fsS --noproxy '*' {} || wget -qO- {}) | sh -s -- {}".format(
        direct, direct, " ".join(shlex.quote(value) for value in values))


def validate_environment(args):
    if not hasattr(os, "geteuid") or os.geteuid() != 0 or not sys.platform.startswith("linux"):
        raise TaskError("调优端需要常规 Linux 的 root 权限")
    if Path("/etc/openwrt_release").exists():
        raise TaskError("OpenWrt / iStoreOS 本次仅支持测速端角色")
    for binary in ("bash", "iperf3", "ip", "tc", "ss", "sysctl", "systemctl"):
        if not shutil.which(binary):
            raise TaskError("缺少依赖: " + binary)
    if args.control_port == args.iperf_port or any(port < 1024 or port > 65535 for port in (args.control_port, args.iperf_port)):
        raise TaskError("接入和测速端口必须不同，且在 1024-65535 之间")
    if not 30 <= args.token_ttl <= 1800:
        raise TaskError("token 有效期必须在 30-1800 秒之间")
    for bandwidth in (args.server_bw, args.client_bw):
        if bandwidth is not None and not 1 <= bandwidth <= 1000000:
            raise TaskError("标称带宽必须在 1-1000000 Mbps 之间")
    family = socket.AF_INET if args.family == 4 else socket.AF_INET6
    try:
        addresses = socket.getaddrinfo(args.server, args.control_port, family, socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise TaskError("无法解析所选协议族的服务器地址: " + str(error))
    args.server = addresses[0][4][0]
    for port in (args.control_port, args.iperf_port):
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            try:
                if family == socket.AF_INET6:
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                sock.bind(("0.0.0.0" if args.family == 4 else "::", port))
            except OSError:
                raise TaskError("端口 {} 已被占用或不能监听，未启动任务".format(port))
    for path in (args.script, args.client_script):
        if not Path(path).is_file():
            raise TaskError("缺少同版本程序文件: " + path)
    if 'VERSION="{}"'.format(VERSION) not in Path(args.script).read_text(encoding="utf-8") or 'TCPFIT_CLIENT_VERSION="{}"'.format(VERSION) not in Path(args.client_script).read_text(encoding="utf-8"):
        raise TaskError("调优模块版本不一致，请安装完整的同一版本")


def print_report(result):
    print("\n回国调优结果", flush=True)
    print("  基础参数：" + ("已保留" if result["base_kept"] else "已恢复调优前配置"))
    print("  判定依据：" + "；".join(result["base_reasons"]))
    print("  全局整形：{} → {} Mbps".format(result["old_rate"] or "无", result["final_rate"] or "无"))
    print("  整形依据：" + result["shape_reason"])
    print("\n  连接数  测量阶段             吞吐 Mbps    重传次数  估算重传比%    空载 ms    满载 ms")
    for streams in (1, 4):
        for label, rows in (("调优前，限速暂关", result["before"]), ("基础候选，限速暂关", result["after"]), ("最终配置", result["final"])):
            row = representative(rows, streams)
            idle = row["latency"]["idle"]["mean_ms"]
            loaded = row["latency"]["loaded"]["mean_ms"]
            print("  {:>4}    {:<16} {:>10.2f} {:>11} {:>12.3f} {:>10} {:>10}".format(
                streams, label, row["receiver_mbps"], row["retransmits"], row["estimated_retrans_pct"],
                "{:.2f}".format(idle) if idle is not None else "未取得",
                "{:.2f}".format(loaded) if loaded is not None else "未取得"))
        before, after = representative(result["before"], streams), representative(result["after"], streams)
        pct = (after["receiver_mbps"] / before["receiver_mbps"] - 1) * 100
        print("          基础候选相对调优前：吞吐 {:+.2f}%，重传 {:+d} 次".format(pct, after["retransmits"] - before["retransmits"]))
        for phase, label in (("idle", "空载"), ("loaded", "满载")):
            start, end = before["latency"][phase]["mean_ms"], after["latency"][phase]["mean_ms"]
            change = "{:+.2f} ms".format(end - start) if start is not None and end is not None else "缺少可比较样本"
            print("          {}握手延迟变化：{}".format(label, change))
    print("\n  调优前与基础候选使用相同的无限速 fq、方向、时长和测速端；各测三次，展示吞吐居中的整组结果。")
    print("  最终配置列来自最终实际配置的有效测量，不将不同整形条件计算成提升百分比。")
    print("  延迟为 TCP 握手耗时的均值；重传为服务器实际 TCP 重传次数，不是丢包率。")
    print("  当前路径带宽估计：{} Mbps；基础推导带宽参考：{} Mbps。".format(result["path_bandwidth"], result["reference_bandwidth"]))
    print("  记录和配置：" + result["record_dir"])
    print("  需要恢复时可使用 tcpfit archive list / tcpfit archive restore <序号>。", flush=True)


def run_task(args):
    with task_lock(args.lock_fd) as fd:
        args.lock_fd = fd
        return run_locked_task(args)


def run_locked_task(args):
    validate_environment(args)
    pending = Path(args.state_dir) / "return-pending.json"
    if pending.exists():
        raise TaskError("存在尚未恢复的回国任务，请先执行 recover: " + str(read_json(pending).get("record_dir")))
    task_id = secrets.token_hex(8)
    record_dir = Path(args.state_dir) / "return" / (time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + task_id)
    record_dir.mkdir(parents=True, mode=0o700)
    (record_dir / "measurements").mkdir(mode=0o700)
    runtime = Path("/run/tcpfit")
    runtime.mkdir(mode=0o700, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="return-", dir=str(runtime)))
    journal = {
        "record_dir": str(record_dir), "run_dir": str(run_dir), "state_dir": args.state_dir,
        "owner_pid": os.getpid(), "owner_stamp": process_stamp(os.getpid()),
        "dirty": False, "committed": False, "restored": False, "done": False,
    }
    journal_path = record_dir / "transaction.json"
    atomic_json(journal_path, journal)
    guard_log = open(str(record_dir / "recovery.log"), "w", encoding="utf-8")
    guardian = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "guard", "--record-dir", str(record_dir)],
                                stdout=guard_log, stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(args.lock_fd,))
    guard_log.close()
    firewall = Firewall(record_dir, args.family, args.control_port, args.iperf_port, task_id)
    coordinator = Coordinator(args, run_dir, record_dir, firewall)
    worker = Worker(args.script, run_dir, args.lock_fd, coordinator.check)
    previous_handlers = {}
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        previous_handlers[sig] = signal.signal(sig, lambda signum, frame: coordinator.fail("调优任务收到取消或断线信号"))
    result = {"status": "running", "version": VERSION, "record_dir": str(record_dir), "server_nominal_mbps": args.server_bw, "client_nominal_mbps": args.client_bw, "cleanup_complete": False}
    before_state = None
    success = False
    try:
        profile = worker.run("profile", quiet=True)[0].splitlines()
        iface, default_rtt, duration, good_pct, accept_pct = profile
        keys = worker.run("keys", quiet=True)[0].splitlines()
        # 配对之前先确认队列可恢复，避免让家宽接入后才发现不支持。
        QueueState.capture(iface)
        firewall.setup()
        result["firewall"] = {"backend": firewall.state["backend"], "manager": firewall.state["manager"],
                              "source_ip_restricted": firewall.state["backend"] != "none"}
        start_http(coordinator)
        print("\n在测速端复制执行下面这一条命令：\n\n{}\n".format(join_command(args, coordinator.token)), flush=True)
        log("token {} 秒内有效，只能配对一次。接入后无需返回调优端操作。".format(args.token_ttl))
        while not coordinator.paired.wait(0.5):
            coordinator.check()
        coordinator.check()
        route = command(["ip", "-{}".format(args.family), "route", "get", coordinator.peer]).stdout.split()
        if "dev" not in route or route[route.index("dev") + 1] != iface:
            raise TaskError("到测速端的路由未使用默认出口网卡，无法套用原版调优，未修改网络参数")
        before_state = Snapshot.capture(iface, keys, args.state_dir)
        atomic_json(record_dir / "before.json", before_state)
        worker.run("snapshot")
        worker.run("archive", "return-before-" + task_id[:8], args.role, "", "", coordinator.peer, task_id, record_dir / "before.json")
        atomic_json(pending, {"record_dir": str(record_dir)})
        journal["dirty"] = True
        atomic_json(journal_path, journal)
        log("保存调优前配置，临时解除已有速率限制")
        if before_state["service_active"]:
            command(["systemctl", "stop", "tcpfit-qdisc.service"])
        worker.run("fq", iface)
        worker.stage = "路径带宽探测"
        bandwidth = worker.run("probe", quiet=True)[0]
        if not bandwidth.isdigit() or int(bandwidth) <= 0:
            raise TaskError("当前路径带宽探测失败或低于可推导范围")
        result["path_bandwidth"] = int(bandwidth)
        log("当前路径可用带宽约 {} Mbps，这不是服务器或家宽套餐上限".format(bandwidth))

        def measure_group(stage, rounds=3):
            start = len(coordinator.results)
            worker.stage = stage
            for index in range(rounds):
                log("{}：第 {}/{} 组".format(stage, index + 1, rounds))
                worker.run("verify", quiet=True)
                coordinator.check()
            rows = coordinator.results[start:]
            if len(rows) != 2 * rounds:
                raise TaskError("验证缺少单连接或四连接有效结果")
            return rows

        result["before"] = measure_group("调优前")
        known = [value for value in (args.server_bw, args.client_bw) if value is not None]
        reference = max(int(bandwidth), min(known)) if known else int(bandwidth)
        idle = [row["latency"]["idle"]["mean_ms"] for row in result["before"] if row["latency"]["idle"]["mean_ms"] is not None]
        rtt = min(2000, max(1, int(math.ceil(statistics.median(idle))))) if idle else int(default_rtt)
        result.update(reference_bandwidth=reference, rtt_ms=rtt, rtt_source="实测 TCP 握手延迟" if idle else "原版默认估值，未取得有效延迟")
        log("复用完整基础调优：带宽参考 {} Mbps，延迟 {} ms（{}）".format(reference, rtt, result["rtt_source"]))
        tune_args = ["--role", args.role, "--bw", str(reference), "--rtt", str(rtt)]
        if reference <= 100:
            tune_args.append("--no-initcwnd")
        worker.run("tune", *tune_args)
        result["after"] = measure_group("基础调优后")
        kept, reasons = base_decision(worker, result["before"], result["after"])
        result.update(base_kept=kept, base_reasons=reasons)
        reference_results = result["after"] if kept else result["before"]
        if not kept:
            log("基础候选未通过：" + "；".join(reasons))
            Snapshot.restore(before_state)
            if before_state["service_active"]:
                command(["systemctl", "stop", "tcpfit-qdisc.service"])
            worker.run("fq", iface)
        else:
            log("基础候选通过：" + reasons[0])
        old_rate = before_state["queue"]["rate"]
        candidate, reason = shape_candidate(worker, old_rate, reference_results)
        result.update(old_rate=old_rate, final_rate=old_rate, shape_candidate=candidate, shape_reason=reason)
        if candidate is not None:
            log(reason)
            worker.run("test-shape", iface, candidate)
            log("按原版方法静置 15 秒后验证候选整形")
            until = time.monotonic() + 15
            while time.monotonic() < until:
                coordinator.check()
                time.sleep(0.5)
            shaped = measure_group("提高整形候选")
            result["shape_measurements"] = shaped
            shape_ok, shape_reasons = shape_decision(worker, reference_results, shaped, old_rate, candidate, int(accept_pct))
            if not shape_ok:
                result["shape_reason"] = "候选未通过，恢复原整形：" + "；".join(shape_reasons)
                QueueState.restore(before_state["queue"])
            else:
                worker.run("shape", candidate)
                result["final_rate"] = candidate
                result["shape_reason"] = "候选通过三组复测，吞吐达到原版可接受档位，重传分档未变差且无重复跳变"
        elif old_rate is not None or not kept or before_state["queue"].get("limited_fq"):
            QueueState.restore(before_state["queue"])
        if result["final_rate"] == old_rate and before_state["service_active"]:
            command(["systemctl", "start", "tcpfit-qdisc.service"])
            QueueState.restore(before_state["queue"])
        # 无旧整形且基础候选通过时，上面三组验证已经针对最终配置，无需重复消耗流量。
        if old_rate is None and kept and not before_state["queue"].get("limited_fq") and not before_state["service_active"]:
            result["final"] = result["after"]
        else:
            result["final"] = measure_group("最终配置验证")
            if result["final_rate"] != old_rate:
                final_ok, final_reasons = shape_decision(worker, reference_results, result["final"], old_rate, candidate, int(accept_pct))
                if not final_ok:
                    QueueState.restore(before_state["queue"])
                    # 候选尚未最终确认，恢复它改写的持久化入口。
                    saved_files = {name: before_state["files"][name] for name in CONFIG_FILES if name in ("/usr/local/sbin/tcpfit-qdisc.sh", "/etc/systemd/system/tcpfit-qdisc.service", "/etc/systemd/system/multi-user.target.wants/tcpfit-qdisc.service")}
                    command(["systemctl", "stop", "tcpfit-qdisc.service"], check=False)
                    Snapshot.restore_files(saved_files)
                    command(["systemctl", "daemon-reload"])
                    if before_state["service_active"]:
                        command(["systemctl", "start", "tcpfit-qdisc.service"])
                    QueueState.restore(before_state["queue"])
                    result["final_rate"] = old_rate
                    result["shape_reason"] = "最终验证未通过，恢复原整形：" + "；".join(final_reasons)
                    result["final"] = measure_group("恢复原整形后验证")
        coordinator.check()
        final_state = Snapshot.capture(iface, keys, args.state_dir)
        if final_state["queue"]["rate"] != result["final_rate"]:
            raise TaskError("最终实际整形值与已验证配置不一致")
        atomic_json(record_dir / "final.json", final_state)
        worker.run("archive", "return-final-" + task_id[:8], args.role, reference, rtt, coordinator.peer, task_id, record_dir / "final.json")
        result["status"] = "completed"
        success = True
    except BaseException as error:
        reason = str(error) or error.__class__.__name__
        coordinator.fail(reason)
        result.update(status="failed", error=reason)
        log("任务未完成，正在恢复本次改动")
    finally:
        coordinator.finished = "OK 测速已结束，调优端正在保存和清理" if success else "FAIL 任务失败，调优端正在恢复配置"
        if coordinator.peer:
            coordinator.done_ack.wait(5)
        cleanup_errors = []
        try:
            coordinator.close()
        except (TaskError, OSError, subprocess.SubprocessError) as error:
            cleanup_errors.append(str(error))
        try:
            Firewall.cleanup(firewall.path)
        except (TaskError, OSError) as error:
            cleanup_errors.append(str(error))
        if cleanup_errors:
            success = False
            result.update(status="failed", error="；".join(cleanup_errors))
        if not success and journal["dirty"] and before_state:
            try:
                Snapshot.restore(before_state)
                journal["restored"] = True
                atomic_json(record_dir / "final.json", Snapshot.capture(iface, keys, args.state_dir))
                log("已恢复调优前参数和队列")
            except (TaskError, OSError) as error:
                cleanup_errors.append(str(error))
                result["recovery_error"] = str(error)
        if not cleanup_errors:
            journal.update(committed=success, done=True)
            clear_pending(journal)
            result["cleanup_complete"] = True
        else:
            journal["recovery_error"] = "；".join(cleanup_errors)
            atomic_json(pending, {"record_dir": str(record_dir)})
            log("恢复或清理未完成，保留恢复记录: " + str(record_dir))
        remove_credentials(run_dir)
        atomic_json(journal_path, journal)
        atomic_json(record_dir / "result.json", result)
        if journal["done"]:
            guardian.wait(timeout=5)
            shutil.rmtree(str(run_dir))
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    if success:
        print_report(result)
    else:
        log("失败原因：" + result.get("error", "未知"))
        log("有效阶段记录和恢复配置：" + str(record_dir))
    return 0 if success else 2


def main():
    parser = argparse.ArgumentParser(description="tcpfit 回国调优协调与恢复工具")
    sub = parser.add_subparsers(dest="command")
    sub.required = True
    run = sub.add_parser("run")
    run.add_argument("--script", required=True)
    run.add_argument("--client-script", required=True)
    run.add_argument("--server", required=True)
    run.add_argument("--control-port", type=int, default=5211)
    run.add_argument("--iperf-port", type=int, default=5212)
    run.add_argument("--family", type=int, choices=(4, 6), default=4)
    run.add_argument("--role", choices=("proxy", "bulk", "mixed"), default="proxy")
    run.add_argument("--token-ttl", type=int, default=600)
    run.add_argument("--server-bw", type=int)
    run.add_argument("--client-bw", type=int)
    run.add_argument("--state-dir", default="/var/lib/tcpfit")
    run.add_argument("--lock-fd", type=int)
    measure = sub.add_parser("measure")
    measure.add_argument("--run-dir", required=True)
    measure.add_argument("--duration", type=int, required=True)
    measure.add_argument("--streams", type=int, choices=(1, 4), required=True)
    measure.add_argument("--stage", required=True)
    for action in ("guard", "recover"):
        child = sub.add_parser(action)
        child.add_argument("--record-dir", required=True)
        if action == "recover":
            child.add_argument("--lock-fd", type=int)
    restore = sub.add_parser("restore")
    restore.add_argument("--snapshot", required=True)
    restore.add_argument("--lock-fd", type=int)
    args = parser.parse_args()
    try:
        if args.command == "run":
            return run_task(args)
        if args.command == "measure":
            request_measurement(args)
        elif args.command == "guard":
            guard(args.record_dir)
        elif args.command == "recover":
            with task_lock(args.lock_fd):
                recover(args.record_dir)
        elif args.command == "restore":
            with task_lock(args.lock_fd):
                pending = Path("/var/lib/tcpfit/return-pending.json")
                if pending.exists():
                    raise TaskError("尚有未恢复的回国任务，请先执行 tcpfit return-recover")
                Snapshot.restore(read_json(args.snapshot))
        return 0
    except (TaskError, OSError, ValueError, subprocess.SubprocessError) as error:
        print("[!] " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
