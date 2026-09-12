#!/usr/bin/env python3
"""优化线路调优的接入、测量和事务管理；网络参数由 tcpfit.sh 的公共函数应用。"""

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
import select
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

VERSION = "0.16.2"
MIB = 1048576
BUFFER_MAX_BYTES = 2147483647
BUFFER_MIN_STEP = MIB
BUFFER_GROW_STEP = 2 * MIB
SPEED_TOLERANCE = 0.05
MEASUREMENT_REPEATS = 2
SPEED_SPREAD = 0.10
RETRANS_TARGETS = {1: (0.5, 1.0), 4: (1.0, 5.0)}
METRIC_EPSILON = 1e-9
BUFFER_KEYS = (
    "net.core.rmem_max", "net.core.wmem_max", "net.core.rmem_default", "net.core.wmem_default",
    "net.ipv4.tcp_rmem", "net.ipv4.tcp_wmem",
)
MAX_BODY = 2 * 1024 * 1024
HEARTBEAT_TIMEOUT = 45
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


def validate_ports(args):
    ports = (args.control_port, args.iperf_port)
    if any(isinstance(port, bool) or not isinstance(port, int) or port != 0 and not 1024 <= port <= 65535 for port in ports):
        raise TaskError("端口必须在 1024-65535 之间，或用 0 自动选择空闲端口")
    if args.control_port and args.control_port == args.iperf_port:
        raise TaskError("接入和测速端口必须不同")


@contextmanager
def reserve_ports(args):
    """保留实际绑定成功的端口，直到交给对应服务或任务退出。"""
    validate_ports(args)
    family = socket.AF_INET if args.family == 4 else socket.AF_INET6
    address = "0.0.0.0" if args.family == 4 else "::"
    reservations = {}
    try:
        # 先保留手动指定的端口，避免自动分配占用另一个指定端口。
        for name in sorted(("control_port", "iperf_port"), key=lambda name: getattr(args, name) == 0):
            port = getattr(args, name)
            sock = socket.socket(family, socket.SOCK_STREAM)
            reservations[name] = sock
            try:
                if family == socket.AF_INET6:
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                sock.bind((address, port))
            except OSError as error:
                if port:
                    raise TaskError("端口 {} 已被占用或不能绑定，未启动任务: {}".format(port, error))
                raise TaskError("无法分配空闲 TCP 端口，未启动任务: " + str(error))
            selected = sock.getsockname()[1]
            if not 1024 <= selected <= 65535:
                raise TaskError("系统分配的端口不在 1024-65535 之间，未启动任务")
            setattr(args, name, selected)
        yield reservations
    finally:
        for sock in reservations.values():
            sock.close()


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
        # 删除根队列后，内核可能已自动重建原来的 fq 0:。这种队列不能用
        # change 寻址；参数一致时保留它，参数不同时用 replace 完整重建。
        if old_major == "0" and root["kind"] != "mq" and len(entries) == 1 and not state["classes"]:
            for line in current.splitlines():
                words = line.split()
                if words[:4] == ["qdisc", root["kind"], "0:", "root"]:
                    if QueueState.parse_options(root["kind"], words[4:]) == root["options"]:
                        return
                    break
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
                if key in BUFFER_KEYS and command(["sysctl", "-n", key]).stdout.split() != value.split():
                    raise TaskError("快照恢复后的缓冲区实际值不一致: " + key)
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
# 优化线路调优流程单独覆盖公式，普通基础调优保持原规则。
TCPFIT_BUFFER_FORMULA="1.5×BDP，试调上限2.5×BDP"
calc_buffer_profile(){
  local bdp maximum initial limit
  bdp=$(calc_bdp "$2" "$3")
  read -r maximum limit < <(awk -v b="$bdp" 'BEGIN{printf "%.0f %.0f\n",int(b*1.5),int(b*2.5)}')
  if ! is_posint "$maximum" 1 2147483647 || ! is_posint "$limit" "$maximum" 2147483647; then
    printf '%s\n' 'BDP 推导的缓冲区超出内核整数范围（1～2147483647 字节），停止优化线路调优' >&2
    return 1
  fi
  initial=$(calc_buf_default "$1" "$bdp")
  [ "$initial" -le "$maximum" ] || initial="$maximum"
  printf '%s %s %s %s\n' "$bdp" "$maximum" "$initial" "$limit"
}
buf_max_reason(){
  printf '%s' '1.5 × BDP，试调上限 2.5 × BDP'
}
action="$1"; shift
case "$action" in
  profile) printf '%s\n' "$(detect_iface)" "$DEFAULT_RTT" "$VDUR" "$VERIFY_GOOD_PCT" "$VERIFY_ACCEPT_PCT" ;;
  keys) printf '%s\n' $TUNED_KEYS ;;
  snapshot) take_snapshot ;;
  sample) run_iperf return-path "$2" "$1" ;;
  tune) cmd_tune "$@" ;;
  buffer-plan) calc_buffer_profile "$1" "$2" "$3" ;;
  buffers) read_buffer_config ;;
  buffer) apply_buffer_config "$@" ;;
  fq) qdisc_remove_root "$1" && qdisc_set_fq "$1" ;;
  test-shape) apply_test_shaper "$1" "$2" ;;
  shape) if [ "$1" = off ]; then cmd_shape --off; else cmd_shape --rate "$1"; fi ;;
  margin) calc_margin "$1" ;;
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
            return "未检测到可用防火墙工具，跳过规则管理，继续测试"
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
    if isinstance(document, float) and not math.isfinite(document):
        return str(document)
    return document


def raw_document(raw):
    try:
        return clean_raw(json.loads(raw))
    except ValueError:
        # 截断 JSON 仍保留诊断内容，移除临时连接标识。
        return {"unparsed": re.sub(r'"cookie"\s*:\s*"[^"]*"?', '"cookie":"已移除"', raw)}


class Coordinator:
    def __init__(self, args, run_dir, record_dir, firewall):
        self.args, self.run_dir, self.record_dir, self.firewall = args, Path(run_dir), Path(record_dir), firewall
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(24)
        self.session = None
        self.peer = None
        self.expires = time.monotonic() + args.token_ttl
        self.last_seen = None
        self.error = None
        self.finished = None
        self.done_ack = threading.Event()
        self.paired = threading.Event()
        self.active = None
        self.results = []
        self.closed = False
        self.httpd = None
        self.port_reservations = {}
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
        if self.peer and self.finished is None and now - self.last_seen > HEARTBEAT_TIMEOUT:
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
                return 410, "临时 token 已过期，请重新启动优化线路调优"
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
            if not re.fullmatch(r"[a-f0-9]{16}", job.get("id", "")):
                raise TaskError("本地测量请求无效")
            if (self.run_dir / ("result-" + job["id"] + ".json")).exists():
                return "WAIT"
            if job.get("kind") == "latency":
                job["latency"] = {}
                self.active = job
                return "LATENCY " + job["id"]
            if job.get("kind", "throughput") != "throughput" or job.get("streams") not in (1, 4) or not 1 <= job.get("duration", 0) <= 600:
                raise TaskError("本地测量请求无效")
            raw_path = self.run_dir / (job["id"] + ".server.json")
            output = open(str(raw_path), "w", encoding="utf-8")
            # iperf3 自行绑定端口，启动前才释放预留；启动失败按原流程清理。
            reservation = self.port_reservations.pop("iperf_port", None)
            if reservation is not None:
                reservation.close()
            try:
                process = subprocess.Popen(
                    ["iperf3", "-{}".format(self.args.family), "-s", "-1", "-J", "-p", str(self.args.iperf_port)],
                    stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                )
            finally:
                output.close()
            entry = {"pid": process.pid, "stamp": process_stamp(process.pid)}
            atomic_json(self.run_dir / "iperf.json", entry)
            context_path = self.run_dir / "measurement-context.json"
            context = read_json(context_path) if context_path.exists() else {}
            job.update(process=process, process_entry=entry, raw_path=raw_path, latency={}, context=context)
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
            log("{}：{} 秒 × {} 连接".format(job["stage"], job["duration"], job["streams"]))
            return "RUN {} {} {}".format(job["id"], job["duration"], job["streams"])

    def measure_idle_latency(self, repeats):
        """计算缓冲区前只采集握手延迟，不启动吞吐测试。"""
        request_path = self.run_dir / "request.json"
        samples = []
        log("采集空载延迟")
        for sample in range(1, repeats + 1):
            self.check()
            if request_path.exists():
                raise TaskError("已有测量正在进行，不能重复采集延迟")
            job_id = secrets.token_hex(8)
            result_path = self.run_dir / ("result-" + job_id + ".json")
            atomic_json(request_path, {"id": job_id, "kind": "latency", "stage": "空载延迟", "sample": sample})
            try:
                deadline = time.monotonic() + 60
                while not result_path.exists():
                    self.check()
                    if time.monotonic() >= deadline:
                        raise TaskError("等待空载延迟结果超时")
                    time.sleep(0.2)
                self.check()
                samples.append(read_json(result_path))
                atomic_json(self.record_dir / "idle-latency.json", samples)
            finally:
                request_path.unlink()
                if result_path.exists():
                    result_path.unlink()
        return samples

    def job_for(self, job_id):
        if not self.active or self.active["id"] != job_id:
            raise TaskError("结果不属于当前测试，或本轮结果已经提交")
        return self.active

    def save_latency(self, job_id, phase, raw):
        with self.lock:
            job = self.job_for(job_id)
            if job.get("kind") == "latency" and phase != "idle":
                raise TaskError("空载延迟任务不能回报满载数据")
            if phase in job["latency"]:
                raise TaskError("本轮延迟数据已经提交")
            job["latency"][phase] = latency_summary(raw)
            if job.get("kind") == "latency":
                atomic_json(self.run_dir / ("result-" + job_id + ".json"),
                            dict(job["latency"][phase], id=job_id, sample=job["sample"]))
                self.active = None

    def result(self, job_id, raw):
        with self.lock:
            job = self.job_for(job_id)
            if job.get("kind") == "latency":
                raise TaskError("空载延迟任务不接受吞吐结果")
            context_path = self.run_dir / "measurement-context.json"
            context = read_json(context_path) if context_path.exists() else job.get("context", {})
            record_path = self.record_dir / "measurements" / (job_id + ".json")
            record = dict(context, id=job_id, stage=job["stage"], streams=job["streams"], status="failed",
                          latency=job["latency"], raw_client=raw_document(raw))
            try:
                try:
                    rc = job["process"].wait(timeout=10)
                except subprocess.TimeoutExpired:
                    raise TaskError("调优端 iperf3 未正常结束")
                record["raw_server"] = raw_document(job["raw_path"].read_text(encoding="utf-8"))
                if rc:
                    raise TaskError("调优端 iperf3 执行失败（退出码 {}）".format(rc))
                try:
                    client, server = json.loads(raw), read_json(job["raw_path"])
                except (ValueError, OSError):
                    raise TaskError("iperf3 未返回完整 JSON 结果")
                measured = parse_measurement(client, server, job["streams"], job["duration"])
                if set(job["latency"]) != {"idle", "loaded"}:
                    raise TaskError("测速端未回报空载和满载延迟采集结果")
            except (TaskError, OSError) as error:
                record["error"] = str(error)
                if "raw_server" not in record and job["raw_path"].exists():
                    try:
                        record["raw_server"] = raw_document(job["raw_path"].read_text(encoding="utf-8"))
                    except OSError as raw_error:
                        record["raw_server_error"] = str(raw_error)
                atomic_json(record_path, record)
                raise
            measured.update(id=job_id, stage=job["stage"], timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), latency=job["latency"])
            record.update(measured, status="valid")
            atomic_json(record_path, record)
            self.results.append(measured)
            atomic_json(self.run_dir / ("result-" + job_id + ".json"), measured)
            job["raw_path"].unlink()
            (self.run_dir / "iperf.json").unlink()
            self.active = None
            log("{:.2f} Mbps，重传 {} 次（{:.3f}%）".format(
                measured["receiver_mbps"], measured["retransmits"], measured["estimated_retrans_pct"]))

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.token, self.session = None, None
            active = self.active
        try:
            if active and active.get("kind") != "latency":
                stop_process(active["process_entry"])
                active["process"].wait(timeout=5)
                record_path = self.record_dir / "measurements" / (active["id"] + ".json")
                if not record_path.exists():
                    record = dict(active.get("context", {}), id=active["id"], stage=active["stage"],
                                  streams=active["streams"], status="failed", error=self.error or "测速未完成",
                                  latency=active["latency"])
                    if active["raw_path"].exists():
                        record["raw_server"] = raw_document(active["raw_path"].read_text(encoding="utf-8"))
                    atomic_json(record_path, record)
        finally:
            for reservation in self.port_reservations.values():
                reservation.close()
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
            if not post and self.path == "/join.ps1":
                return self.reply(200, windows_client_script(coordinator.args).read_bytes(), "text/plain; charset=utf-8")
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
    reservation = coordinator.port_reservations.get("control_port")
    if reservation is None:
        server = Server(("0.0.0.0" if family == socket.AF_INET else "::", coordinator.args.control_port), Handler)
    else:
        server = Server(reservation.getsockname(), Handler, bind_and_activate=False)
        server.socket.close()
        server.socket = reservation
        coordinator.port_reservations.pop("control_port")
        try:
            # HTTP 服务直接接管已绑定的套接字，不释放后重新绑定。
            server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.server_name = socket.getfqdn(server.server_address[0])
            server.server_port = server.server_address[1]
            server.server_activate()
        except BaseException:
            server.server_close()
            raise
    server.coordinator = coordinator
    coordinator.args.control_port = server.server_address[1]
    try:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    except BaseException:
        server.server_close()
        raise
    coordinator.httpd = server


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


def median_metric(results, streams, key):
    values = [positive(row.get(key), key, key == "estimated_retrans_pct")
              for row in results if row.get("streams") == streams and row.get("status", "valid") == "valid"]
    if not values:
        raise TaskError("缺少 {} 连接的有效 {}".format(streams, key))
    return statistics.median(values)


def retrans_tolerance(baseline):
    """估算重传比以百分点计；忽略小幅相对波动，并限制绝对容差。"""
    return max(0.01, min(0.1, baseline * 0.2))


def measurement_issues(results, streams, repeats=None, *, check_stability=True):
    group = [row for row in results if row.get("streams") == streams]
    expected = repeats if repeats is not None else max([row.get("repeats", 2) for row in group] or [2])
    if len(group) < expected or any(row.get("status", "valid") != "valid" for row in group):
        return ["{} 连接失败、跳过或未完成验证".format(streams)]
    if len({row.get("config_id") for row in group}) > 1:
        return ["{} 连接混用了不同配置下的数据".format(streams)]
    try:
        speed = [positive(row.get("receiver_mbps"), "接收速度") for row in group]
        retrans = [positive(row.get("estimated_retrans_pct"), "估算重传比", True) for row in group]
    except TaskError as error:
        return [str(error)]
    if not check_stability:
        return []
    reasons = []
    if max(speed) - min(speed) > statistics.median(speed) * SPEED_SPREAD + METRIC_EPSILON:
        reasons.append("{} 连接接收速度明显波动，不稳定".format(streams))
    if max(retrans) - min(retrans) > max(0.1, statistics.median(retrans) * 0.5) + METRIC_EPSILON:
        reasons.append("{} 连接估算重传比明显波动，不稳定".format(streams))
    return reasons


def speed_issues(baseline, measured, streams):
    if baseline is None:
        return ["尚未取得稳定测速参照"]
    if median_metric(measured, streams, "receiver_mbps") + METRIC_EPSILON < (
            median_metric(baseline, streams, "receiver_mbps") * (1 - SPEED_TOLERANCE)):
        return ["{} 连接速度比稳定参照下降超过 5%".format(streams)]
    return []


def mode_goal(baseline, measured, streams):
    return (not measurement_issues(measured, streams)
            and not speed_issues(baseline, measured, streams)
            and median_metric(measured, streams, "estimated_retrans_pct") <= RETRANS_TARGETS[streams][1])


def retrans_acceptable(worker, before, after, tolerant=False, modes=(1,)):
    reasons = []
    for streams in modes:
        issues = measurement_issues(before, streams) + measurement_issues(after, streams)
        if issues:
            reasons.extend(issues)
            continue
        baseline = median_metric(before, streams, "estimated_retrans_pct")
        current = median_metric(after, streams, "estimated_retrans_pct")
        if current > baseline + retrans_tolerance(baseline) + METRIC_EPSILON:
            reasons.append("{} 连接的估算重传比明显上升（{:.3f}% → {:.3f}%）".format(streams, baseline, current))
    return reasons


def base_decision(worker, before, after):
    if before is None:
        return False, ["未取得稳定测速参照，无法确认候选效果"]
    reasons = measurement_issues(before, 1) + measurement_issues(after, 1)
    if not reasons:
        reasons.extend(speed_issues(before, after, 1))
        if median_metric(after, 1, "estimated_retrans_pct") > RETRANS_TARGETS[1][1]:
            reasons.append("单连接估算重传比超过 1%")
    return not reasons, reasons or ["测速稳定，重传不超过 1%，速度下降不超过 5%"]


def buffers_from_sysctl(values):
    state = {}
    for key in BUFFER_KEYS:
        fields = str(values.get(key, "")).split()
        count = 3 if key in ("net.ipv4.tcp_rmem", "net.ipv4.tcp_wmem") else 1
        if len(fields) != count or any(not value.isdigit() or not 0 < int(value) <= 2147483647 for value in fields):
            raise TaskError("缺少有效的缓冲区参数: " + key)
        state[key] = [int(value) for value in fields]
        if count == 3 and state[key] != sorted(state[key]):
            raise TaskError("缓冲区最小值、默认值和上限顺序无效: " + key)
    return state


def read_buffers(worker):
    values = dict(line.split("=", 1) for line in worker.run("buffers", quiet=True)[0].splitlines())
    return buffers_from_sysctl(values)


def format_mib(value):
    if value < 1024:
        return "{} 字节".format(value)
    if value < MIB:
        return "{:.2f}".format(value / 1024).rstrip("0").rstrip(".") + " KiB"
    return "{:.2f}".format(value / MIB).rstrip("0").rstrip(".") + " MiB"


def describe_buffers(state):
    return "接收 {} / 发送 {}".format(
        format_mib(state["net.ipv4.tcp_rmem"][2]), format_mib(state["net.ipv4.tcp_wmem"][2]))


def describe_rate_limits(queue):
    limits = []
    if queue.get("rate") is not None:
        limits.append("总限速 {:g} Mbps".format(queue["rate"]))
    flow_rates = set()
    for entry in queue.get("qdiscs", []):
        options = entry.get("options", [])
        if entry["kind"] == "fq" and "maxrate" in options:
            value = options[options.index("maxrate") + 1]
            if value != "unlimited":
                flow_rates.add(QueueState.rate_mbps(value))
    if flow_rates:
        limits.append("单连接限速 {} Mbps".format(" / ".join("{:g}".format(rate) for rate in sorted(flow_rates))))
    return "；".join(limits) or "未设置"


def check_buffer_target(state, maximum, initial=None):
    if any(state[key][-1] != maximum for key in (
            "net.core.rmem_max", "net.core.wmem_max", "net.ipv4.tcp_rmem", "net.ipv4.tcp_wmem")):
        raise TaskError("缓冲区实际上限与本轮目标不一致")
    if initial is not None and any(state[key][index] != initial for key, index in (
            ("net.core.rmem_default", 0), ("net.core.wmem_default", 0),
            ("net.ipv4.tcp_rmem", 1), ("net.ipv4.tcp_wmem", 1))):
        raise TaskError("缓冲区实际默认值与本轮目标不一致")


def apply_buffers(worker, maximum, initial=None):
    args = (maximum,) if initial is None else (maximum, initial)
    worker.run("buffer", *args, quiet=True)
    state = read_buffers(worker)
    check_buffer_target(state, maximum, initial)
    return state


def describe_measurements(measured):
    parts = []
    for streams in (1, 4):
        if any(row.get("streams") == streams for row in measured):
            try:
                detail = "{} 连接接收 {:.2f} Mbps / 估算重传比 {:.3f}%".format(
                    streams, median_metric(measured, streams, "receiver_mbps"),
                    median_metric(measured, streams, "estimated_retrans_pct"))
            except TaskError:
                detail = "{} 连接未取得完整结果".format(streams)
            issues = measurement_issues(measured, streams)
            parts.append(detail + ("（{}）".format("；".join(issues)) if issues else ""))
    return "；".join(parts)


def next_buffer_target(current, limit, direction, step, visited, minimum):
    """优先沿当前方向缩步；跳过已测上限继续向边界搜索，再尝试反向。"""

    for candidate_direction in (direction, -direction):
        candidate_step = step if candidate_direction == direction else BUFFER_MIN_STEP
        while True:
            target = min(limit, max(minimum, current + candidate_direction * candidate_step))
            if target != current and target not in visited:
                return target, candidate_direction, abs(target - current)
            if candidate_step <= BUFFER_MIN_STEP:
                break
            candidate_step = max(BUFFER_MIN_STEP, candidate_step // 2)
        target = min(limit, max(minimum, current + candidate_direction * BUFFER_MIN_STEP))
        while target != current:
            if target not in visited:
                return target, candidate_direction, abs(target - current)
            if target in (minimum, limit):
                break
            target = min(limit, max(minimum, target + candidate_direction * BUFFER_MIN_STEP))
    return None


def next_buffer_growth_target(current, limit, visited):
    """重传不高时按 2 MiB 试更大上限，跳过已测候选。"""
    target = min(limit, current + BUFFER_GROW_STEP)
    while target > current:
        if target not in visited:
            return target, 1, target - current
        if target == limit:
            break
        target = min(limit, target + BUFFER_GROW_STEP)
    return None


def next_buffer_refine_target(previous_candidate, retained, visited):
    """从高重传一侧按 1 MiB 向下微调，只测高于已保留上限的候选。"""
    target = previous_candidate - BUFFER_MIN_STEP
    while target > retained:
        if target not in visited:
            return target, -1, previous_candidate - target
        target -= BUFFER_MIN_STEP
    return None


def buffer_improvement(baseline, before, after, streams, before_max, after_max):
    if measurement_issues(after, streams) or speed_issues(baseline, after, streams):
        return []
    current = median_metric(after, streams, "estimated_retrans_pct")
    if current <= RETRANS_TARGETS[streams][1]:
        if after_max > before_max:
            return ["{} 连接重传不超过 1%，速度合格，优先保留更大缓冲区".format(streams)]
        if not mode_goal(baseline, before, streams):
            return ["{} 连接重传降至不超过 1%，速度合格".format(streams)]
        return []
    # 高重传时只暂留下调后的明确改善，继续寻找重传合格的配置。
    if after_max >= before_max or measurement_issues(before, streams):
        return []
    previous = median_metric(before, streams, "estimated_retrans_pct")
    if previous - current > retrans_tolerance(previous) + METRIC_EPSILON:
        return ["{} 连接估算重传比下降（{:.3f}% → {:.3f}%）".format(streams, previous, current)]
    return []


def restore_buffers(worker, state):
    restored = apply_buffers(worker, state["net.ipv4.tcp_rmem"][2], state["net.ipv4.tcp_rmem"][1])
    if restored != state:
        raise TaskError("缓冲区未完整恢复到上一组值")
    return restored


def tune_buffers(worker, baseline, measured, state, limit, measure_group, trials, trial_path, search=None):
    """持续试调未测上限；初值不稳定时以首个稳定候选建立比较参照。"""
    maximum, initial = state["net.ipv4.tcp_rmem"][2], state["net.ipv4.tcp_rmem"][1]
    check_buffer_target(state, maximum, initial)
    minimum = max(state[key][0] for key in ("net.ipv4.tcp_rmem", "net.ipv4.tcp_wmem"))
    if not 1 <= minimum <= maximum <= limit <= BUFFER_MAX_BYTES:
        raise TaskError("初始缓冲区不在本机允许的试调范围内")
    streams = 1
    issues = measurement_issues(baseline, streams, check_stability=False)
    if issues:
        raise TaskError("初值测速数据无效：" + "；".join(issues))
    reference_round = 0
    if measurement_issues(baseline, streams):
        log("初值测速不稳定，继续试调；取得稳定结果后再建立比较参照")
        baseline, reference_round = None, None
    measured = [row for row in measured if row["streams"] == streams]
    phase = {"phase": 1, "streams": streams, "status": "checking", "entry_measurements": measured}
    no_gain = 0
    log("开始试调：重传不超过 1% 时，收发缓冲区上限每次各增加 2 MiB，最高 2.5 × BDP；遇高重传后每次各减少 1 MiB 微调")
    atomic_json(trial_path, trials)
    visited = {maximum}
    direction = -1
    high = median_metric(measured, streams, "estimated_retrans_pct") > RETRANS_TARGETS[streams][1]
    high_targets = {maximum} if high and not measurement_issues(measured, streams) else set()
    refine_from = None
    step = (2 if high else 1) * MIB
    reason = "单连接{}，目标 ≤ {:.3f}%".format(
        "高重传，优先下调" if high else "测速尚未稳定，优先下调", RETRANS_TARGETS[streams][1])
    if baseline is None:
        reason = "初值测速不稳定，先下调寻找稳定配置"
    while True:
        previous_max = state["net.ipv4.tcp_rmem"][2]
        growing = (not measurement_issues(measured, streams)
                   and median_metric(measured, streams, "estimated_retrans_pct") <= RETRANS_TARGETS[streams][1])
        reference_max = previous_max
        refining = False
        if growing:
            if refine_from is None:
                refine_from = min((value for value in high_targets if value > previous_max), default=None)
            refining = refine_from is not None
            if refining:
                reference_max = refine_from
                reason = "已触及高重传边界，向下微调缓冲区"
                next_target = next_buffer_refine_target(refine_from, previous_max, visited)
            else:
                reason = "单连接重传不高，尝试更大的缓冲区"
                next_target = next_buffer_growth_target(previous_max, limit, visited)
        else:
            next_target = next_buffer_target(previous_max, limit, direction, step, visited, minimum)
        if next_target is None:
            if refining:
                stop_reason = "高重传边界的 1 MiB 微调已完成，没有高于保留配置的未测候选"
            elif growing:
                stop_reason = "已达 2.5 × BDP 试调上限或更高候选均已测过"
            else:
                stop_reason = "单连接可调范围内的候选均已测过"
            break
        target, next_direction, actual_step = next_target
        if not growing and next_direction != direction:
            reason = "当前方向无可用邻点，反向细调；" + reason
        direction, step = next_direction, actual_step
        visited.add(target)
        index = len(trials) + 1
        action = "上调" if direction > 0 else "下调"
        log("第 {} 轮，单连接：{}；{}：{} → {}（各{} {}）".format(
            index, reason, "候选收发缓冲区上限" if refining else "收发缓冲区上限",
            format_mib(reference_max), format_mib(target),
            "增加" if direction > 0 else "减少", format_mib(step)))
        log("保留配置：" + describe_measurements(measured))
        trial = {"round": index, "phase": 1, "streams": streams, "reason": reason,
                 "direction": action, "before": state, "before_measurements": measured,
                 "target_max_bytes": target, "step_bytes": step, "status": "applying"}
        if refining:
            trial["refine_from_max_bytes"] = reference_max
        trials.append(trial)
        atomic_json(trial_path, trials)
        try:
            candidate = apply_buffers(worker, target, min(initial, target))
            trial.update(actual=candidate, status="measuring")
            atomic_json(trial_path, trials)
            rows = measure_group("单连接试调第 {} 轮".format(index), modes=(streams,))
            trial["measurements"] = rows
            log("候选结果：" + describe_measurements(rows))
            invalid = measurement_issues(rows, streams, check_stability=False)
            if invalid:
                raise TaskError("候选测速数据无效：" + "；".join(invalid))
            reasons = measurement_issues(rows, streams)
            gains = []
            if not reasons:
                if median_metric(rows, streams, "estimated_retrans_pct") > RETRANS_TARGETS[streams][1]:
                    high_targets.add(target)
                if baseline is None:
                    # 首次稳定结果只建立参照，不声称相对不稳定初值提速或降低重传。
                    baseline, reference_round = rows, index
                    trial["established_reference"] = True
                    gains = ["首次取得稳定测速，暂留并作为后续比较参照"]
                    log("已建立稳定测速参照：" + describe_measurements(rows))
                else:
                    reasons.extend(speed_issues(baseline, rows, streams))
                    gains = buffer_improvement(baseline, measured, rows, streams, previous_max, target) if not reasons else []
                if target > previous_max and median_metric(rows, streams, "estimated_retrans_pct") > RETRANS_TARGETS[streams][1]:
                    reasons.append("单连接估算重传比超过 1%，不保留更大缓冲区")
            kept = bool(gains) and not reasons
            reasons = reasons or gains or ["未取得重传改善或符合条件的更大缓冲区"]
            for row in rows:
                row["decision"] = ("已保留" if kept else "已回退") + "：" + "；".join(reasons)
            trial.update(kept=kept, reasons=reasons, no_gain_rounds=0 if kept else no_gain + 1,
                         status="kept" if kept else "restoring")
            atomic_json(trial_path, trials)
            if not kept:
                trial["restored"] = restore_buffers(worker, state)
                trial["status"] = "rejected"
                atomic_json(trial_path, trials)
        except BaseException as error:
            trial.update(kept=False, status="failed", error=str(error) or error.__class__.__name__)
            try:
                trial["restored"] = restore_buffers(worker, state)
                log("第 {} 轮失败或中断：{}；已回退至 {}".format(index, trial["error"], describe_buffers(state)))
            except BaseException as restore_error:
                trial["rollback_error"] = str(restore_error) or restore_error.__class__.__name__
                log("本轮回退未完成，交由整任务快照恢复：" + trial["rollback_error"])
            atomic_json(trial_path, trials)
            raise
        if refining:
            refine_from = target
        if kept:
            no_gain = 0
            state, measured = candidate, rows
            log("本轮暂留：{}；单连接，收发上限 {} → {}".format(
                "；".join(reasons), format_mib(previous_max), format_mib(target)))
            reason = "单连接继续细调重传，重传不高后上调试探更大缓冲区"
        else:
            no_gain += 1
            log("本轮回退：{}；单连接，收发上限 {} → {}；连续无收益 {} 轮，继续试调".format(
                "；".join(reasons), format_mib(target), format_mib(previous_max), no_gain))
            if growing:
                direction, step = 1, BUFFER_GROW_STEP
            elif step > BUFFER_MIN_STEP:
                step = max(BUFFER_MIN_STEP, step // 2)
                reason = "上一轮效果未通过，回退后减小每次调整量"
            else:
                direction, step = -direction, BUFFER_MIN_STEP
                reason = "上一轮无收益，回退后反向细调"
    reached = mode_goal(baseline, measured, streams)
    phase.update(status="completed" if reached else "stopped", goal_reached=reached, measurements=measured,
                 stop_reason=stop_reason)
    if search is not None:
        search.update(rounds=len(trials), speed_reference_round=reference_round,
                      speed_tolerance=SPEED_TOLERANCE, prefer_larger=True,
                      growth_step_bytes=BUFFER_GROW_STEP, refine_step_bytes=BUFFER_MIN_STEP,
                      order=[streams], phases=[phase], min_bytes=minimum,
                      retrans_targets={str(streams): {"excellent": RETRANS_TARGETS[streams][0], "high": RETRANS_TARGETS[streams][1]}},
                      stop_reason=stop_reason)
    if trials:
        trials[-1]["search_stop_reason"] = stop_reason
    atomic_json(trial_path, trials)
    log("缓冲区试调停止：{}；当前 {}".format(stop_reason, describe_buffers(state)))
    return measured, state, baseline


def shape_candidate(worker, old_rate, results):
    if old_rate is None:
        return None, "原本没有全局整形，本次不创建"
    issues = measurement_issues(results, 4)
    if issues:
        return None, "四连接未取得完整稳定数据，恢复原整形：" + "；".join(issues)
    samples = [row["receiver_mbps"] for row in results if row["streams"] == 4]
    if min(samples) <= old_rate:
        return None, "{} 次四连接实测未全部超过旧上限，恢复原整形".format(len(samples))
    stable = int(min(samples))
    rate = stable - int(worker.run("margin", stable, quiet=True)[0])
    if rate <= old_rate:
        return None, "按原版安全余量计算后未高于旧值，恢复原整形"
    if rate > 100000:
        return None, "候选值超过原版整形支持范围，恢复原整形"
    return rate, "{} 次四连接实测均超过旧上限；扣除原版安全余量，只验证 {} Mbps".format(len(samples), rate)


def shape_decision(worker, reference, measured, old_rate, candidate, accept_pct):
    reasons = retrans_acceptable(worker, reference, measured, modes=(4,))
    if measurement_issues(measured, 4):
        return False, reasons
    if median_metric(measured, 4, "receiver_mbps") < candidate * accept_pct / 100:
        reasons.append("四连接吞吐未达到原版可接受的整形值 {}%".format(accept_pct))
    if min(row["receiver_mbps"] for row in measured if row["streams"] == 4) <= old_rate:
        reasons.append("提高整形后没有稳定超过旧上限")
    return not reasons, reasons

def configuration_identity(snapshot):
    """时间戳和自动路由剩余寿命不改变配置；其余快照内容全部参与匹配。"""
    identity = {key: value for key, value in snapshot.items() if key != "captured_at"}
    identity["queue"] = queue_parameters(snapshot.get("queue", {}))
    identity["routes"] = {family: [Snapshot.route_config(line) for line in lines]
                          for family, lines in snapshot.get("routes", {}).items()}
    return json.dumps(identity, ensure_ascii=False, sort_keys=True)


def configuration_parameters(snapshot):
    """合并实际参数相同的结果；每次测量的完整快照仍单独保留。"""
    parameters = {
        "sysctl": {key: str(value).split() for key, value in snapshot.get("sysctl", {}).items()},
        "queue": queue_parameters(snapshot.get("queue", {})),
        "routes": {family: sorted(Snapshot.route_config(line) for line in lines)
                   for family, lines in snapshot.get("routes", {}).items()},
    }
    return json.dumps(parameters, ensure_ascii=False, sort_keys=True)


def queue_parameters(queue):
    """句柄由内核分配；按层级和选项比较，忽略显示统计。"""
    handles = {entry.get("handle"): "q{}".format(index)
               for index, entry in enumerate(queue.get("qdiscs", [])) if entry.get("handle")}
    handles.update({entry.get("handle"): "c{}".format(index)
                    for index, entry in enumerate(queue.get("classes", [])) if entry.get("handle")})

    def parent_identity(parent):
        if parent in handles:
            return handles[parent]
        if parent and ":" in parent:
            major, minor = parent.split(":", 1)
            if not major:
                root = next((entry for entry in queue.get("qdiscs", []) if entry.get("parent") == "root"), {})
                major = root.get("handle", "0:").split(":", 1)[0]
            if major + ":" in handles:
                return handles[major + ":"] + ":" + minor
        return parent

    return {"rate": queue.get("rate"), "limited_fq": queue.get("limited_fq"),
            "qdiscs": [(entry["kind"], parent_identity(entry.get("parent")), entry["options"])
                       for entry in queue.get("qdiscs", [])],
            "classes": [(parent_identity(entry.get("parent")), entry["options"])
                        for entry in queue.get("classes", [])]}


class MeasurementBook:
    """每次测量关联完整快照；失败和计划中未执行的测量也保留序号。"""

    def __init__(self, worker, coordinator, record_dir, capture, repeats, duration):
        self.worker, self.coordinator = worker, coordinator
        self.record_dir, self.capture = Path(record_dir), capture
        self.repeats, self.duration = repeats, duration
        self.records, self.configurations, self.identities = [], {}, {}
        (self.record_dir / "configurations").mkdir(exist_ok=True)

    def register(self, snapshot=None):
        snapshot = self.capture() if snapshot is None else snapshot
        identity = configuration_identity(snapshot)
        if identity in self.identities:
            return self.identities[identity]
        config_id = "C{}".format(len(self.configurations) + 1)
        self.identities[identity] = config_id
        self.configurations[config_id] = snapshot
        atomic_json(self.record_dir / "configurations" / (config_id + ".json"), snapshot)
        return config_id

    def save(self):
        atomic_json(self.record_dir / "measurement-index.json", self.records)

    def measure_group(self, stage, modes=(1,), repeats=None):
        repeats = self.repeats if repeats is None else repeats
        config_id = self.register()
        buffers = buffers_from_sysctl(self.configurations[config_id]["sysctl"])
        rows = []
        for streams in modes:
            for sample in range(1, repeats + 1):
                row = {"number": len(self.records) + 1, "stage": stage, "streams": streams,
                       "sample": sample, "repeats": repeats, "config_id": config_id, "buffers": buffers,
                       "status": "pending", "decision": "未完成验证"}
                self.records.append(row)
                rows.append(row)
        self.save()
        try:
            for row in rows:
                self.coordinator.check()
                row["status"] = "measuring"
                self.save()
                self.worker.stage = "{} 第 {}/{} 次".format(stage, row["sample"], repeats)
                context_path = Path(self.coordinator.run_dir) / "measurement-context.json"
                atomic_json(context_path, row)
                start = len(self.coordinator.results)
                self.worker.run("sample", row["streams"], self.duration, quiet=True)
                self.coordinator.check()
                measured = self.coordinator.results[start:]
                if len(measured) != 1 or measured[0].get("streams") != row["streams"]:
                    raise TaskError("本次未返回指定连接数的唯一有效结果")
                row.update(measured[0], stage=stage, status="valid", decision="有效，待综合判定")
                self.save()
            for streams in modes:
                issues = measurement_issues(rows, streams, repeats)
                for row in rows:
                    if row["streams"] == streams:
                        row["stability"] = "unstable" if issues else "stable"
                        if issues:
                            row["decision"] = "；".join(issues)
                        else:
                            ratio = median_metric(rows, streams, "estimated_retrans_pct")
                            excellent, high = RETRANS_TARGETS[streams]
                            band = "重传优秀" if ratio <= excellent else "高重传" if ratio > high else "重传未达优秀"
                            row["decision"] = band
            self.save()
            log(stage + "完成：" + describe_measurements(rows))
            return rows
        except BaseException as error:
            reason = str(error) or error.__class__.__name__
            for row in rows:
                if row["status"] == "measuring":
                    row.update(status="failed", decision="失败：" + reason)
                    active = getattr(self.coordinator, "active", None)
                    if active:
                        row["id"] = active["id"]
                elif row["status"] == "pending":
                    row.update(status="skipped", decision="跳过：前序测量失败或任务中断")
                elif row["status"] == "valid":
                    row["decision"] = "本次有效，整组未完成验证"
            self.save()
            raise
        finally:
            context_path = Path(self.coordinator.run_dir) / "measurement-context.json"
            if context_path.exists():
                context_path.unlink()


def configuration_groups(book, recommendation=None):
    groups = {}

    def group_for(config_id):
        snapshot = book.configurations[config_id]
        identity = configuration_parameters(snapshot)
        if identity not in groups:
            groups[identity] = {"number": len(groups) + 1, "config_id": config_id,
                                "config_ids": [], "measurements": [],
                                "buffers": buffers_from_sysctl(snapshot["sysctl"])}
        group = groups[identity]
        if config_id not in group["config_ids"]:
            group["config_ids"].append(config_id)
        return group

    for row in book.records:
        group = group_for(row["config_id"])
        group["measurements"].append(row)
        if row["status"] == "valid":
            group["config_id"] = row["config_id"]
    if recommendation is not None:
        group = group_for(recommendation)
        group["config_id"] = recommendation
    return list(groups.values())


def configuration_verdict(group, recommendation=None):
    rows = group["measurements"]
    flags = ["推荐"] if recommendation in group["config_ids"] else []
    if not rows:
        return " / ".join(flags + ["未测速"])
    if any(row["status"] != "valid" for row in rows):
        flags.append("未完成")
        if any(row["status"] == "failed" for row in rows):
            flags.append("失败")
        if any(row["status"] == "skipped" for row in rows):
            flags.append("跳过")
    valid = [row for row in rows if row["status"] == "valid"]
    if valid:
        # 同一实际参数可以对应多个快照，稳定性比较不受存档编号影响。
        comparable = [dict(row, config_id=None) for row in rows]
        if (any(row.get("stability") == "unstable" for row in valid)
                or len(valid) == len(rows) and any(measurement_issues(comparable, streams)
                                                   for streams in {row["streams"] for row in valid})):
            flags.append("不稳定")
        if "回退" in valid[-1].get("decision", "") and "推荐" not in flags:
            flags.append("已回退")
        if not flags:
            ratios = [(median_metric(valid, streams, "estimated_retrans_pct"), RETRANS_TARGETS[streams])
                      for streams in {row["streams"] for row in valid}]
            flags.append("重传优秀" if all(ratio <= targets[0] for ratio, targets in ratios)
                         else "重传偏高" if any(ratio > targets[1] for ratio, targets in ratios) else "有效")
    return " / ".join(flags) or "未完成"


def measurement_table(book, recommendation=None):
    groups = configuration_groups(book, recommendation)
    modes = sorted({row["streams"] for row in book.records}) or [1]
    show_rates = any(describe_rate_limits(book.configurations[group["config_id"]]["queue"]) != "未设置" for group in groups)
    headers = ["序号", "收发上限 MiB"]
    if show_rates:
        headers.append("限速")
    headers.extend("{}连接 Mbps / 重传 %（中位数）".format("单" if streams == 1 else "四") for streams in modes)
    headers.append("判定")
    print("\n" + " | ".join(headers))
    for group in groups:
        buffers = group["buffers"]
        cells = [str(group["number"]), "{:g}/{:g}".format(buffers["net.ipv4.tcp_rmem"][2] / MIB,
                                                        buffers["net.ipv4.tcp_wmem"][2] / MIB)]
        if show_rates:
            cells.append(describe_rate_limits(book.configurations[group["config_id"]]["queue"]))
        for streams in modes:
            metrics = []
            for key, precision in (("receiver_mbps", 2), ("estimated_retrans_pct", 3)):
                try:
                    metrics.append("{:.{}f}".format(median_metric(group["measurements"], streams, key), precision))
                except TaskError:
                    metrics.append("未取得")
            cells.append(" / ".join(metrics))
        cells.append(configuration_verdict(group, recommendation))
        print(" | ".join(cells))
    print(flush=True)
    return groups


def select_configuration(book, recommendation, automatic=False, reader=None, check=None):
    groups = configuration_groups(book, recommendation)
    recommended_number = next(group["number"] for group in groups if recommendation in group["config_ids"])
    if automatic:
        log("保存推荐配置 {}".format(recommended_number))
        return recommendation, None
    # 调优协调进程在后台启动，必须从控制终端读取，不能依赖后台标准输入。
    terminal = None
    if reader is None:
        try:
            terminal = open("/dev/tty", encoding="utf-8")
        except OSError as error:
            raise TaskError("无法读取保存选择，请在终端运行或使用 --yes 自动保存推荐配置") from error

        def reader():
            print("选择配置序号 [推荐 {}，回车确认]：".format(recommended_number), end="", flush=True)
            while True:
                if check:
                    check()
                if select.select([terminal], [], [], 0.5)[0]:
                    line = terminal.readline()
                    if not line:
                        raise TaskError("保存选择输入已关闭")
                    return line
    try:
        while True:
            value = reader().strip()
            if not value:
                return recommendation, None
            normalized = value.lstrip("0") or "0"
            if re.fullmatch(r"[0-9]+", value) and len(normalized) <= len(str(len(groups))):
                number = int(normalized)
                group = next((item for item in groups if item["number"] == number), None)
                if group and (recommendation in group["config_ids"] or any(row["status"] == "valid" for row in group["measurements"])):
                    return group["config_id"], number
            log("请输入表中可选的配置序号")
    finally:
        if terminal:
            terminal.close()


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
        result.update(recovered=True, base_kept=False, recovery_message="已恢复任务开始前的配置")
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


def windows_client_script(args):
    return Path(args.client_script).with_name("tcpfit-client.ps1")


def join_command(args, token, platform="linux"):
    host = "[{}]".format(args.server) if args.family == 6 else args.server
    if platform == "windows":
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        direct = quote("http://{}:{}/join.ps1".format(host, args.control_port))
        values = " ".join(quote(value) for value in (args.server, args.control_port, args.iperf_port, token))
        # 在独立作用域中下载、按 UTF-8 解码并执行，兼容 PowerShell 5.1/7。
        return ("& { $w = New-Object Net.WebClient; $w.Proxy = $null; $w.Encoding = [Text.Encoding]::UTF8; "
                "try { & ([scriptblock]::Create($w.DownloadString(" + direct + "))) " + values +
                " } finally { $w.Dispose() } }")
    if platform != "linux":
        raise ValueError("未知测速端平台: " + platform)
    direct = shlex.quote("http://{}:{}/join.sh".format(host, args.control_port))
    values = [args.server, str(args.control_port), str(args.iperf_port), token]
    # 两种下载工具都直接获取调优端提供的同版本脚本，无需发布标签或证书。
    return "(curl -fsS --noproxy '*' {} || wget -qO- {}) | sh -s -- {}".format(
        direct, direct, " ".join(shlex.quote(value) for value in values))


def reference_bandwidth(server_bw, client_bw):
    known = [value for value in (server_bw, client_bw) if value is not None]
    if any(isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000000 for value in known):
        raise TaskError("标称带宽必须是 1-1000000 Mbps 的整数")
    return min(known) if known else None


def validate_environment(args):
    if not hasattr(os, "geteuid") or os.geteuid() != 0 or not sys.platform.startswith("linux"):
        raise TaskError("调优端需要常规 Linux 的 root 权限")
    if Path("/etc/openwrt_release").exists():
        raise TaskError("OpenWrt / iStoreOS 本次仅支持测速端角色")
    for binary in ("bash", "iperf3", "ip", "tc", "ss", "sysctl", "systemctl"):
        if not shutil.which(binary):
            raise TaskError("缺少依赖: " + binary)
    validate_ports(args)
    if not 30 <= args.token_ttl <= 1800:
        raise TaskError("token 有效期必须在 30-1800 秒之间")
    if isinstance(args.repeats, bool) or not isinstance(args.repeats, int) or not 1 <= args.repeats <= 10:
        raise TaskError("测速次数必须是 1-10 的整数")
    reference_bandwidth(args.server_bw, args.client_bw)
    family = socket.AF_INET if args.family == 4 else socket.AF_INET6
    try:
        addresses = socket.getaddrinfo(args.server, args.control_port, family, socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise TaskError("无法解析所选协议族的服务器地址: " + str(error))
    args.server = addresses[0][4][0]
    for path in (Path(args.script), Path(args.client_script), windows_client_script(args)):
        if not Path(path).is_file():
            raise TaskError("缺少同版本程序文件: " + str(path))
    versions = ((Path(args.script), 'VERSION="{}"'),
                (Path(args.client_script), 'TCPFIT_CLIENT_VERSION="{}"'),
                (windows_client_script(args), "$TCPFIT_CLIENT_VERSION = '{}'"))
    for path, marker in versions:
        if marker.format(VERSION) not in path.read_text(encoding="utf-8-sig"):
            raise TaskError("调优模块版本不一致，请安装完整的同一版本")


def print_report(result):
    print("\n已保存配置 {}（推荐 {}）".format(result["selected_number"], result["recommended_number"]), flush=True)
    print("  最终缓冲区：" + describe_buffers(result["buffers_final"]))
    print("  判定：" + result["selected_validation"])
    print("  当前限速：" + result["rate_limits_final"])
    print("  试调 {} 轮；停止原因：{}".format(len(result["buffer_trials"]), result["buffer_search"]["stop_reason"]))
    print("  记录：" + result["record_dir"], flush=True)


def persist_selected_configuration(worker, snapshot):
    """将手选测量时的实际参数写入持久化配置，避免临时 fq 与旧整形文件冲突。"""
    name = CONFIG_FILES[0]
    saved = snapshot["files"].get(name)
    content = base64.b64decode(saved["data"]).decode("utf-8") if saved and "data" in saved else ""
    remaining = dict(snapshot["sysctl"])
    lines = []
    for line in content.splitlines():
        match = re.match(r"\s*([a-z0-9_.]+)\s*=", line)
        key = match.group(1) if match else None
        if key in remaining:
            lines.append("{} = {}".format(key, remaining.pop(key)))
        elif key not in snapshot["sysctl"]:
            lines.append(line)
    lines.extend("{} = {}".format(key, value) for key, value in remaining.items())
    entry = dict(saved) if saved else {"mode": 0o644, "uid": 0, "gid": 0}
    entry["data"] = base64.b64encode(("\n".join(lines) + "\n").encode("utf-8")).decode("ascii")
    Snapshot.restore_files({name: entry})
    queue = snapshot["queue"]
    if snapshot.get("service_active"):
        return
    if queue["rate"] is not None:
        worker.run("shape", queue["rate"])
    elif not queue.get("limited_fq") and all(item["kind"] == "fq" for item in queue.get("qdiscs", [])):
        worker.run("shape", "off")

def run_task(args):
    with task_lock(args.lock_fd) as fd:
        args.lock_fd = fd
        return run_locked_task(args)


def run_locked_task(args):
    validate_environment(args)
    pending = Path(args.state_dir) / "return-pending.json"
    if pending.exists():
        raise TaskError("存在尚未恢复的优化线路调优任务，请先执行 recover: " + str(read_json(pending).get("record_dir")))
    with reserve_ports(args) as reservations:
        return run_prepared_task(args, reservations)


def run_prepared_task(args, reservations):
    reference = reference_bandwidth(args.server_bw, args.client_bw)
    pending = Path(args.state_dir) / "return-pending.json"
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
    coordinator.port_reservations = reservations
    worker = Worker(args.script, run_dir, args.lock_fd, coordinator.check)
    previous_handlers = {}
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        previous_handlers[sig] = signal.signal(sig, lambda signum, frame: coordinator.fail("调优任务收到取消或断线信号"))
    result = {"status": "running", "version": VERSION, "record_dir": str(record_dir), "server_nominal_mbps": args.server_bw, "client_nominal_mbps": args.client_bw, "cleanup_complete": False,
              "control_port": args.control_port, "iperf_port": args.iperf_port}
    before_state = None
    book = None
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
        log("接入 / 测速端口: {} / {} TCP".format(args.control_port, args.iperf_port))
        print("\n按测速端系统复制执行对应命令：\n\nLinux / OpenWrt / iStoreOS：\n{}\n\nWindows PowerShell：\n{}\n".format(
            join_command(args, coordinator.token), join_command(args, coordinator.token, "windows")), flush=True)
        log("token {} 秒内有效，只能配对一次。测速自动执行，结束后在调优端选择保存配置。".format(args.token_ttl))
        while not coordinator.paired.wait(0.5):
            coordinator.check()
        coordinator.check()
        route = command(["ip", "-{}".format(args.family), "route", "get", coordinator.peer]).stdout.split()
        if "dev" not in route or route[route.index("dev") + 1] != iface:
            raise TaskError("到测速端的路由未使用默认出口网卡，无法套用原版调优，未修改网络参数")
        before_state = Snapshot.capture(iface, keys, args.state_dir)
        result["buffers_before"] = buffers_from_sysctl(before_state["sysctl"])
        atomic_json(record_dir / "before.json", before_state)
        worker.run("snapshot")
        worker.run("archive", "return-before-" + task_id[:8], args.role, "", "", coordinator.peer, task_id, record_dir / "before.json")
        atomic_json(pending, {"record_dir": str(record_dir)})
        journal["dirty"] = True
        atomic_json(journal_path, journal)
        result["rate_limits_before"] = describe_rate_limits(before_state["queue"])
        log("当前限速：" + result["rate_limits_before"])
        log("已保存当前配置" + ("，临时解除限速" if result["rate_limits_before"] != "未设置" else ""))
        if before_state["service_active"]:
            command(["systemctl", "stop", "tcpfit-qdisc.service"])
        worker.run("fq", iface)
        book = MeasurementBook(worker, coordinator, record_dir,
                               lambda: Snapshot.capture(iface, keys, args.state_dir), args.repeats, int(duration))
        original_config = book.register(before_state)
        result.update(measurements=book.records, repeats=args.repeats, original_config=original_config)
        measure_group = book.measure_group
        source = "已填标称带宽的较小值"
        if reference is None:
            log("两端标称带宽均留空，使用四连接探测当前路径带宽")
            probe = measure_group("路径带宽探测", modes=(4,))
            issues = measurement_issues(probe, 4)
            if issues:
                raise TaskError("路径带宽探测不稳定或不完整：" + "；".join(issues))
            goodput = median_metric(probe, 4, "receiver_mbps")
            granularity = 1 if goodput < 50 else 10 if goodput < 200 else 50
            reference = int(goodput / granularity + 0.5) * granularity
            if reference <= 0:
                raise TaskError("当前路径带宽探测低于可推导范围")
            result["path_bandwidth"] = reference
            source = "四连接实测路径带宽"
        result["bandwidth_source"] = source
        log("带宽参考 {} Mbps（{}）".format(reference, source))
        result["idle_latency"] = coordinator.measure_idle_latency(args.repeats)
        idle = [sample["mean_ms"] for sample in result["idle_latency"] if sample["mean_ms"] is not None]
        rtt = min(2000, max(1, int(math.ceil(statistics.median(idle))))) if idle else int(default_rtt)
        result.update(reference_bandwidth=reference, rtt_ms=rtt, rtt_source="实测 TCP 握手延迟" if idle else "原版默认估值，未取得有效延迟")
        log("空载延迟 {} ms（{}）".format(rtt, "实测" if idle else "默认估值"))
        bdp, maximum, initial, limit = map(int, worker.run("buffer-plan", args.role, reference, rtt, quiet=True)[0].split())
        result["buffer_plan"] = {"bdp_bytes": bdp, "max_bytes": maximum, "default_bytes": initial,
                                 "limit_bytes": limit}
        log("BDP {}，初始收发上限 {}，试调上限 {}".format(format_mib(bdp), format_mib(maximum), format_mib(limit)))
        tune_args = ["--role", args.role, "--bw", str(reference), "--rtt", str(rtt)]
        if reference <= 100:
            tune_args.append("--no-initcwnd")
        worker.run("tune", *tune_args)
        current_buffers = read_buffers(worker)
        check_buffer_target(current_buffers, maximum, initial)
        result["buffer_plan"]["min_bytes"] = max(current_buffers[key][0] for key in ("net.ipv4.tcp_rmem", "net.ipv4.tcp_wmem"))
        result["buffers_derived"] = current_buffers
        log("已应用初值：" + describe_buffers(current_buffers))
        result["initial_after"] = measure_group("初值测速")
        result["buffer_trials"] = []
        result["buffer_search"] = {}
        result["stage_measurements"], result["buffers_selected"], speed_reference = tune_buffers(
            worker, result["initial_after"], result["initial_after"], current_buffers, limit,
            measure_group, result["buffer_trials"], record_dir / "buffer-trials.json", result["buffer_search"])
        result["speed_reference"] = None
        if speed_reference is not None:
            result["speed_reference"] = "initial_after" if result["buffer_search"]["speed_reference_round"] == 0 else "first_stable_after"
            if result["speed_reference"] == "first_stable_after":
                result["first_stable_after"] = speed_reference
        result["after"] = result["stage_measurements"]
        kept, reasons = base_decision(worker, speed_reference, result["after"])
        result.update(base_kept=kept, base_reasons=reasons)
        for row in result["after"]:
            row["decision"] = ("已保留" if kept else "已回退") + "：" + "；".join(reasons)
        if not kept:
            log("基础候选未通过：" + "；".join(reasons))
            Snapshot.restore(before_state)
            log("已恢复调优前缓冲区：" + describe_buffers(result["buffers_before"]))
            if before_state["service_active"]:
                command(["systemctl", "stop", "tcpfit-qdisc.service"])
            worker.run("fq", iface)
        else:
            log("保留 TCP 缓冲区：" + describe_buffers(result["buffers_selected"]))
        old_rate = before_state["queue"]["rate"]
        # 仅已有全局整形需要四连接数据；在实际保留的缓冲区下独立测量。
        shape_reference = measure_group("已有整形提高检查", modes=(4,)) if old_rate is not None else []
        result["shape_reference"] = shape_reference
        candidate, reason = shape_candidate(worker, old_rate, shape_reference)
        result.update(old_rate=old_rate, final_rate=old_rate, shape_candidate=candidate, shape_reason=reason)
        if candidate is not None:
            log(reason)
            worker.run("test-shape", iface, candidate)
            log("按原版方法静置 15 秒后验证候选整形")
            until = time.monotonic() + 15
            while time.monotonic() < until:
                coordinator.check()
                time.sleep(0.5)
            shaped = measure_group("提高整形候选", modes=(4,))
            result["shape_measurements"] = shaped
            shape_ok, shape_reasons = shape_decision(worker, shape_reference, shaped, old_rate, candidate, int(accept_pct))
            for row in shaped:
                row["decision"] = "整形候选暂留，待最终验收" if shape_ok else "整形候选已回退：" + "；".join(shape_reasons)
            if not shape_ok:
                result["shape_reason"] = "候选未通过，恢复原整形：" + "；".join(shape_reasons)
                QueueState.restore(before_state["queue"])
            else:
                worker.run("shape", candidate)
                result["final_rate"] = candidate
                result["shape_reason"] = "候选通过四连接 {} 次复测，吞吐合格且估算重传比未明显变差".format(args.repeats)
        elif old_rate is not None or not kept or before_state["queue"].get("limited_fq"):
            QueueState.restore(before_state["queue"])
        if result["final_rate"] == old_rate and before_state["service_active"]:
            command(["systemctl", "start", "tcpfit-qdisc.service"])
            QueueState.restore(before_state["queue"])
        # 参数未变化时直接采用保留组的测速结果；队列变化后才需要复测。
        current_state = Snapshot.capture(iface, keys, args.state_dir)
        measured_state = book.configurations[result["after"][0]["config_id"]]
        if kept and configuration_parameters(current_state) == configuration_parameters(measured_state):
            result["final"] = result["after"]
        else:
            result["final"] = measure_group("最终配置验证", modes=(1, 4) if result["final_rate"] != old_rate else (1,))
            if result["final_rate"] != old_rate:
                final_ok, final_reasons = shape_decision(worker, shape_reference, result["final"], old_rate, candidate, int(accept_pct))
                if not final_ok:
                    for row in result["final"]:
                        row["decision"] = "整形最终验收失败，已回退：" + "；".join(final_reasons)
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
        final_ok, final_reasons = base_decision(worker, speed_reference, result["final"])
        for row in result["final"]:
            row["decision"] = ("通过" if final_ok else "已回退") + "：" + "；".join(final_reasons)
        if not final_ok and (kept or result["final_rate"] != old_rate):
            result["rejected_final"] = result["final"]
            Snapshot.restore(before_state)
            result.update(base_kept=False, base_reasons=final_reasons, final_rate=old_rate)
            result["shape_reason"] = "最终验收未通过，恢复原配置和原整形"
            result["final"] = measure_group("最终验收未通过，恢复原配置复测")
            kept = False
        final_state = Snapshot.capture(iface, keys, args.state_dir)
        expected_buffers = result["buffers_selected"] if kept else result["buffers_before"]
        if buffers_from_sysctl(final_state["sysctl"]) != expected_buffers:
            raise TaskError("最终实际缓冲区值与已验证配置不一致")
        if final_state["queue"]["rate"] != result["final_rate"]:
            raise TaskError("最终实际整形值与已验证配置不一致")
        recommendation = book.register(final_state)
        result["recommended_config"] = recommendation
        goal_reasons = []
        if not mode_goal(speed_reference, result["final"], 1):
            goal_reasons.append("单连接未达到重传不超过 1%、测速稳定和速度保护要求")
        result["goal_reasons"] = goal_reasons
        result["recommended_validation"] = "通过" if kept else "已恢复原配置"
        result["recommended_measurements"] = result["final"]
        book.save()
        groups = measurement_table(book, recommendation)
        result["configuration_options"] = [{"number": group["number"], "config_id": group["config_id"],
                                            "config_ids": group["config_ids"],
                                            "measurement_numbers": [row["number"] for row in group["measurements"]]}
                                           for group in groups]
        result["recommended_number"] = next(group["number"] for group in groups if recommendation in group["config_ids"])
        if goal_reasons:
            log("单连接尚未达到重传、稳定性或速度要求")
        coordinator.finished = "OK 测速已结束，请在调优端选择保存配置"
        selected, number = select_configuration(book, recommendation, args.yes, check=coordinator.check)
        result.update(selected_config=selected, selected_number=number or result["recommended_number"],
                      selection="automatic" if number is None else "manual",
                      selected_validation=result["recommended_validation"])
        if selected != recommendation:
            selected_state = book.configurations[selected]
            Snapshot.restore(selected_state)
            persist_selected_configuration(worker, selected_state)
            final_state = Snapshot.capture(iface, keys, args.state_dir)
            if any(final_state["sysctl"].get(key, "").split() != value.split()
                   for key, value in selected_state["sysctl"].items()):
                raise TaskError("手选配置的实际参数与测速快照不一致")
            if queue_parameters(final_state["queue"]) != queue_parameters(selected_state["queue"]):
                raise TaskError("手选配置的实际整形与测速快照不一致")
            selected_group = next(group for group in groups if group["number"] == number)
            result["final"] = [row for row in selected_group["measurements"] if row["status"] == "valid"]
            result["selected_validation"] = "手动选择，试调判定：" + configuration_verdict(selected_group)
            result["final_rate"] = final_state["queue"]["rate"]
            result["shape_reason"] = "按配置序号 {} 保存".format(number)
            log("已选择配置 {}".format(number))
        result["buffers_final"] = buffers_from_sysctl(final_state["sysctl"])
        result["rate_limits_final"] = describe_rate_limits(final_state["queue"])

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
                restored_state = Snapshot.capture(iface, keys, args.state_dir)
                restored_buffers = buffers_from_sysctl(restored_state["sysctl"])
                if restored_buffers != result["buffers_before"]:
                    raise TaskError("快照恢复后的实际缓冲区与调优前不一致")
                atomic_json(record_dir / "final.json", restored_state)
                journal["restored"] = True
                result.update(base_kept=False, base_reasons=["任务失败或中断，已恢复调优前配置"],
                              buffers_final=restored_buffers, final_rate=restored_state["queue"]["rate"])
                log("已恢复调优前参数和队列；缓冲区：" + describe_buffers(restored_buffers))
            except (TaskError, OSError) as error:
                cleanup_errors.append(str(error))
                result["recovery_error"] = str(error)
        if not cleanup_errors:
            journal.update(committed=success, done=True)
            clear_pending(journal)
            result["cleanup_complete"] = True
            log("本次接入和测速服务已停止，端口已释放，临时防火墙规则已撤销")
        else:
            journal["recovery_error"] = "；".join(cleanup_errors)
            atomic_json(pending, {"record_dir": str(record_dir)})
            log("恢复或清理未完成，保留恢复记录: " + str(record_dir))
        remove_credentials(run_dir)
        atomic_json(journal_path, journal)
        if book is not None:
            book.save()
        atomic_json(record_dir / "result.json", result)
        if journal["done"]:
            guardian.wait(timeout=5)
            shutil.rmtree(str(run_dir))
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    if success:
        print_report(result)
    else:
        if book is not None:
            measurement_table(book)
        log("失败原因：" + result.get("error", "未知"))
        log("有效阶段记录和恢复配置：" + str(record_dir))
    return 0 if success else 2


def main():
    parser = argparse.ArgumentParser(description="tcpfit 优化线路调优协调与恢复工具")
    sub = parser.add_subparsers(dest="command")
    sub.required = True
    run = sub.add_parser("run")
    run.add_argument("--script", required=True)
    run.add_argument("--client-script", required=True)
    run.add_argument("--server", required=True)
    run.add_argument("--control-port", type=int, default=12223, help="接入端口，默认 TCP 12223")
    run.add_argument("--iperf-port", type=int, default=12224, help="测速端口，默认 TCP 12224")
    run.add_argument("--family", type=int, choices=(4, 6), default=4)
    run.add_argument("--role", choices=("proxy", "bulk", "mixed"), default="proxy")
    run.add_argument("--token-ttl", type=int, default=600)
    run.add_argument("--repeats", type=int, choices=range(1, 11), default=MEASUREMENT_REPEATS)
    run.add_argument("--yes", action="store_true")
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
                    raise TaskError("尚有未恢复的优化线路调优任务，请先执行 tcpfit return-recover")
                Snapshot.restore(read_json(args.snapshot))
        return 0
    except (TaskError, OSError, ValueError, subprocess.SubprocessError) as error:
        print("[!] " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
