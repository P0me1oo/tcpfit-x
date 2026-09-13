#!/bin/sh
# tcpfit 优化线路调优测速端：兼容常规 Linux、OpenWrt、iStoreOS 的 /bin/sh。
# 本脚本只安装缺少的工具、主动连接和回报测量；不修改网络参数或启动入站服务。
TCPFIT_CLIENT_VERSION="0.18.1"
set -u
umask 077

work=""; lock_dir=""; own_lock=0; session=""; iperf_pid=""; latency_pid=""; guard_pid=""; cfg=""; base=""

proc_stamp(){
  proc_line=$(cat "/proc/$1/stat" 2>/dev/null) || return 1
  proc_rest=${proc_line##*) }
  set -- $proc_rest
  [ $# -ge 20 ] && [ "$1" != Z ] || return 1
  shift 19
  printf '%s' "$1"
}

start_guard(){
  parent_pid=$$
  parent_stamp=$(proc_stamp "$parent_pid") || abort "无法读取 /proc 中的进程信息，未开始配对"
  (
    trap - EXIT
    trap '' HUP INT
    trap 'exit 0' TERM
    while [ "$(proc_stamp "$parent_pid")" = "$parent_stamp" ]; do sleep 1; done
    # SIGKILL 不执行主进程的退出 trap；凭据和子进程由独立守护收尾。
    for name in iperf.pid latency.pid; do
      if [ -f "$work/$name" ] && read -r child_pid child_stamp < "$work/$name"; then
        case "$child_pid" in ''|*[!0-9]*) continue ;; esac
        if [ -n "$child_stamp" ] && [ "$(proc_stamp "$child_pid")" = "$child_stamp" ]; then
          kill "$child_pid" 2>/dev/null || true
        fi
      fi
    done
    if [ -f "$cfg" ]; then
      curl --config "$cfg" --max-time 3 --request POST --data-binary '测速端进程异常退出' "$base/error" >/dev/null 2>&1 || true
    fi
    rm -rf "$work"
    rm -f "$lock_dir/pid"
    rmdir "$lock_dir" 2>/dev/null || true
  ) >/dev/null 2>&1 &
  guard_pid=$!
}

cleanup(){
  [ -z "$iperf_pid" ] || kill "$iperf_pid" 2>/dev/null || true
  [ -z "$latency_pid" ] || kill "$latency_pid" 2>/dev/null || true
  [ -z "$iperf_pid" ] || wait "$iperf_pid" 2>/dev/null || true
  [ -z "$latency_pid" ] || wait "$latency_pid" 2>/dev/null || true
  [ -z "$work" ] || rm -rf "$work"
  if [ "$own_lock" = 1 ]; then rm -f "$lock_dir/pid"; rmdir "$lock_dir" 2>/dev/null || true; fi
  [ -z "$guard_pid" ] || kill "$guard_pid" 2>/dev/null || true
  [ -z "$guard_pid" ] || wait "$guard_pid" 2>/dev/null || true
}

abort(){
  message="$1"; code="${2:-1}"
  printf '[!] %s\n' "$message" >&2
  if [ -n "$session" ] && [ -f "$cfg" ]; then
    curl --config "$cfg" --max-time 3 --request POST --data-binary "$message" "$base/error" >/dev/null 2>&1 || true
  fi
  exit "$code"
}

prepare(){
  missing=""
  command -v curl >/dev/null 2>&1 || missing="$missing curl"
  command -v iperf3 >/dev/null 2>&1 || missing="$missing iperf3"
  [ -n "$missing" ] || return 0
  [ "$(id -u)" = 0 ] || abort "需要安装$missing，请以 root 执行接入命令"
  printf '[*] 安装测速工具:%s\n' "$missing"
  if command -v opkg >/dev/null 2>&1; then
    opkg update && opkg install $missing || abort "opkg 安装失败，尚未配对"
  elif command -v apk >/dev/null 2>&1; then
    apk add $missing || abort "apk 安装失败，尚未配对"
  elif command -v apt-get >/dev/null 2>&1; then
    if command -v debconf-set-selections >/dev/null 2>&1; then
      printf 'iperf3 iperf3/start_daemon boolean false\n' | debconf-set-selections
    fi
    apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $missing || abort "apt 安装失败，尚未配对"
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y $missing || abort "dnf 安装失败，尚未配对"
  elif command -v yum >/dev/null 2>&1; then
    yum install -y $missing || abort "yum 安装失败，尚未配对"
  elif command -v pacman >/dev/null 2>&1; then
    pacman -S --needed --noconfirm $missing || abort "pacman 安装失败，尚未配对"
  else
    abort "无法自动安装，请先安装$missing 后重新执行接入命令"
  fi
}

api(){
  method="$1"; endpoint="$2"; shift 2
  status=$(curl --config "$cfg" --request "$method" -o "$work/response" -w '%{http_code}' "$@" "$base$endpoint") || {
    printf '[!] 无法连接调优端，或服务器证书指纹不符\n' >&2
    return 1
  }
  reply=$(cat "$work/response")
  if [ "$status" != 200 ]; then
    printf '[!] %s（HTTP %s）\n' "$reply" "$status" >&2
    return 1
  fi
}

sample_once(){
  # time_connect 减去 DNS 解析时间，得到本次 TCP 握手耗时，单位为秒。
  # 使用字面 IP 和每次新连接，空载、满载均测同一个服务器控制端口。
  sample_value=$(curl --config "$cfg" --fail --max-time 5 -o /dev/null -w '%{time_connect} %{time_namelookup}\n' "$base/ping" 2>/dev/null) || return 0
  printf '%s\n' "$sample_value"
}

sample_idle(){
  job="$1"
  case "$job" in ''|*[!a-f0-9]*) abort "调优端发送了无效测试编号" ;; esac
  [ "${#job}" = 16 ] || abort "测试编号长度不符"
  : > "$work/idle"
  for sample in 1 2 3 4 5; do sample_once >> "$work/idle"; done
  api POST "/latency/$job/idle" --data-binary "@$work/idle" || abort "空载延迟回报失败"
}

run_test(){
  job="$1"; duration="$2"; streams="$3"
  case "$duration" in ''|*[!0-9]*) abort "测试时长无效" ;; esac
  [ "$duration" -ge 1 ] && [ "$duration" -le 600 ] || abort "测试时长超出范围"
  case "$streams" in 1|4) ;; *) abort "只接受单连接或四连接下载测试" ;; esac
  printf '[*] 测速：%s 秒 × %s 连接\n' "$duration" "$streams"
  sample_idle "$job"
  iperf3 "$family" -c "$server" -p "$iperf_port" -R -P "$streams" -t "$duration" -J > "$work/iperf.json" 2> "$work/iperf.err" &
  iperf_pid=$!
  printf '%s %s\n' "$iperf_pid" "$(proc_stamp "$iperf_pid")" > "$work/iperf.pid"
  deadline=$(( $(date +%s) + duration + 25 ))
  (
    trap 'exit 0' INT TERM HUP
    sleep 1
    while kill -0 "$iperf_pid" 2>/dev/null; do
      if [ "$(date +%s)" -ge "$deadline" ]; then kill "$iperf_pid" 2>/dev/null; exit 1; fi
      sample_once
      sleep 1
    done
  ) > "$work/loaded" &
  latency_pid=$!
  printf '%s %s\n' "$latency_pid" "$(proc_stamp "$latency_pid")" > "$work/latency.pid"
  test_rc=0
  wait "$iperf_pid" || test_rc=$?
  iperf_pid=""
  rm -f "$work/iperf.pid"
  wait "$latency_pid" || true
  latency_pid=""
  rm -f "$work/latency.pid"
  if [ "$test_rc" != 0 ]; then
    # 不回报失败 JSON，调优端恢复尚未确认的改动。
    detail=$(cat "$work/iperf.err")
    abort "iperf3 执行失败（退出码 $test_rc）：${detail:-连接中断或服务不可达}"
  fi
  api POST "/latency/$job/loaded" --data-binary "@$work/loaded" || abort "满载延迟回报失败"
  api POST "/result/$job" --data-binary "@$work/iperf.json" || abort "测量结果被拒绝或回报失败"
  printf '[+] 本轮结果已回报，等待后续任务\n'
}

main(){
  if [ "${1:-}" = --help ] || [ $# = 0 ]; then
    printf '%s\n' '这是优化线路调优测速端脚本。请执行调优端生成的完整接入命令。' \
      '用法: sh tcpfit-client.sh 服务器IP 接入端口 测速端口 临时token'
    return 0
  fi
  [ $# = 4 ] || abort "接入参数不完整，请重新复制调优端的命令"
  server="$1"; control_port="$2"; iperf_port="$3"; token="$4"
  case "$server" in ''|*[!a-fA-F0-9.:]*) abort "接入地址必须是调优端生成的 IP 地址" ;; esac
  case "$control_port:$iperf_port" in *[!0-9:]*|:*) abort "端口格式无效" ;; esac
  [ "$control_port" -ge 1024 ] && [ "$control_port" -le 65535 ] && [ "$iperf_port" -ge 1024 ] && [ "$iperf_port" -le 65535 ] || abort "端口超出范围"
  case "$token" in ''|*[!a-zA-Z0-9_-]*) abort "临时 token 格式无效" ;; esac
  [ "${#token}" = 32 ] || abort "临时 token 长度无效"
  case "$server" in *:*) family=-6; base="http://[$server]:$control_port" ;; *) family=-4; base="http://$server:$control_port" ;; esac
  lock_dir="${TMPDIR:-/tmp}/tcpfit-client-$(id -u).lock"
  mkdir "$lock_dir" 2>/dev/null || abort "本机已有测速端任务或未清理的任务锁：$lock_dir；不会抢占现有任务"
  own_lock=1
  printf '%s\n' "$$" > "$lock_dir/pid"
  work=$(mktemp -d "${TMPDIR:-/tmp}/tcpfit-client.XXXXXX") || abort "无法创建临时目录"
  cfg="$work/curl.conf"
  start_guard
  prepare
  {
    printf '%s\n' 'silent' 'show-error' 'noproxy = "*"' 'connect-timeout = 5' 'max-time = 20'
    printf 'header = "X-Tcpfit-Version: %s"\n' "$TCPFIT_CLIENT_VERSION"
    printf 'header = "Authorization: Pair %s"\n' "$token"
  } > "$cfg"
  printf '[*] 正在连接调优端并认证\n'
  api POST /pair --data-binary '' || abort "配对失败，未开始测速"
  case "$reply" in 'OK '*) session="${reply#OK }" ;; *) abort "调优端的配对响应无效" ;; esac
  case "$session" in ''|*[!a-f0-9]*) session=""; abort "会话凭据格式无效" ;; esac
  [ "${#session}" = 48 ] || { session=""; abort "会话凭据长度无效"; }
  sed '/Authorization:/d' "$cfg" > "$cfg.new"
  printf 'header = "Authorization: Bearer %s"\n' "$session" >> "$cfg.new"
  mv "$cfg.new" "$cfg"
  token=""
  printf '[+] 已配对，后续自动测速；无需返回调优端操作\n'
  while true; do
    api GET /next || abort "与调优端的会话中断"
    case "$reply" in
      WAIT) sleep 1 ;;
      'LATENCY '*)
        set -- $reply
        [ $# = 2 ] || abort "延迟采集请求格式无效"
        printf '[*] 采集空载延迟\n'
        sample_idle "$2" ;;
      'RUN '*)
        set -- $reply
        [ $# = 4 ] || abort "测试请求格式无效"
        run_test "$2" "$3" "$4" ;;
      'DONE '* )
        outcome="${reply#DONE }"
        session=""
        case "$outcome" in
          'OK '*) printf '[+] %s\n' "${outcome#OK }"; return 0 ;;
          'FAIL '*) abort "${outcome#FAIL }" 2 ;;
          *) abort "结束状态无效" ;;
        esac ;;
      'FAIL '*) abort "${reply#FAIL }" ;;
      *) abort "无法识别调优端请求" ;;
    esac
  done
}

trap cleanup EXIT
trap 'abort "测速端收到取消信号" 130' INT TERM HUP
main "$@"
