#!/usr/bin/env bash
# tcpfit 一键部署
#
# 两种用法：
#
#   1) 在目标 VPS 上直接跑（只装 agent, 单机调优）
#      curl -fsSL https://raw.githubusercontent.com/P0me1oo/tcpfit-x/main/install.sh | bash
#      然后: tcpfit detect
#
#   2) 在控制端跑（装完整项目, 管理多台机器 —— 未上线, 尚未在真实环境验证）
#      curl -fsSL https://raw.githubusercontent.com/P0me1oo/tcpfit-x/main/install.sh | bash -s -- --full
#      然后: cd /opt/tcpfit && python3 orchestrator/fleet.py detect

set -euo pipefail

REPO="P0me1oo/tcpfit-x"
RAW="https://raw.githubusercontent.com/$REPO/main"
PREFIX="${PREFIX:-/usr/local/bin}"
PROJECT_DIR="${PROJECT_DIR:-/opt/tcpfit}"
MODE=agent

while [ $# -gt 0 ]; do
  case "$1" in
    --full)   MODE=full; shift ;;
    --agent)  MODE=agent; shift ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --dir)    PROJECT_DIR="$2"; shift 2 ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

say(){ printf '\033[0;36m[*]\033[0m %s\n' "$*"; }
ok(){  printf '\033[0;32m[+]\033[0m %s\n' "$*"; }
die(){ printf '\033[0;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "需要 root"
command -v curl >/dev/null || die "需要 curl"

if [ "$MODE" = agent ]; then
  say "安装 agent 到 $PREFIX"
  command -v sha256sum >/dev/null || die "需要 sha256sum 校验安装文件"
  dl=$(mktemp -d)
  trap 'rm -rf "$dl"' EXIT
  curl -fsSL --max-time 30 "$RAW/SHA256SUMS" -o "$dl/SHA256SUMS" || die "下载校验清单失败"
  for f in tcpfit.sh tcpfit-return.py tcpfit-client.sh; do
    curl -fsSL --max-time 60 "$RAW/$f" -o "$dl/$f" || die "下载 $f 失败"
    (cd "$dl" && grep -F "  $f" SHA256SUMS | sha256sum -c - >/dev/null 2>&1) || die "$f 校验失败，未安装"
  done
  version=$(sed -n 's/^VERSION="\([0-9][0-9.]*\)"$/\1/p' "$dl/tcpfit.sh")
  [ -n "$version" ] || die "无法读取版本号"
  lib_dir="/usr/local/lib/tcpfit/$version"
  mkdir -p "$PREFIX" "$lib_dir"
  for f in tcpfit.sh tcpfit-return.py tcpfit-client.sh; do
    install -m 755 "$dl/$f" "$lib_dir/$f.new"
    mv -f "$lib_dir/$f.new" "$lib_dir/$f"
  done
  install -m 755 "$dl/tcpfit.sh" "$PREFIX/tcpfit.new"
  mv -f "$PREFIX/tcpfit.new" "$PREFIX/tcpfit"
  rm -f /usr/local/sbin/tcpfit.sh          # 清掉 v0.3.1 及更早的安装位置
  ok "已安装: $PREFIX/tcpfit"

  # sweep 需要 iperf3, 这里只提示不强装（装包属于会改系统的操作）
  if ! command -v iperf3 >/dev/null 2>&1; then
    echo
    say "sweep 子命令需要 iperf3, 当前未安装. 要用的话："
    if   command -v apt-get >/dev/null; then echo "    apt-get install -y iperf3"
    elif command -v dnf     >/dev/null; then echo "    dnf install -y iperf3"
    elif command -v yum     >/dev/null; then echo "    yum install -y epel-release && yum install -y iperf3"
    elif command -v apk     >/dev/null; then echo "    apk add iperf3 coreutils procps"
    else echo "    用你的包管理器装 iperf3"; fi
  fi

  echo
  echo "下一步："
  echo "    tcpfit detect                       # 看机器画像"
  echo "    tcpfit return                       # 家宽主动接入的回国调优"
  echo "    tcpfit tune --role proxy --bw 500   # 基础调优"
  echo "    tcpfit sweep --peer <近处iperf3服务器> --nominal 500"
  echo "    tcpfit shape --rate <sweep给的推荐值>"
  echo "    tcpfit rollback                     # 随时可回滚"
  exit 0
fi

# ── 完整项目 ────────────────────────────────────────────────────────────────
say "部署完整项目到 $PROJECT_DIR"

if command -v git >/dev/null 2>&1; then
  if [ -d "$PROJECT_DIR/.git" ]; then
    say "已存在, 拉取更新"
    git -C "$PROJECT_DIR" pull --ff-only || die "更新失败, 本地可能有改动"
  else
    git clone --depth 1 "https://github.com/$REPO.git" "$PROJECT_DIR" || die "克隆失败"
  fi
else
  say "无 git, 改用 tarball"
  mkdir -p "$PROJECT_DIR"
  curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/main" \
    | tar xz -C "$PROJECT_DIR" --strip-components=1 || die "下载失败"
fi

chmod +x "$PROJECT_DIR/tcpfit.sh" "$PROJECT_DIR/orchestrator/fleet.py" 2>/dev/null || true
ok "项目已就绪: $PROJECT_DIR"

# 控制端依赖
miss=()
python3 -c 'import yaml' 2>/dev/null || miss+=("PyYAML")
command -v ssh  >/dev/null || miss+=("openssh-client")
command -v scp  >/dev/null || miss+=("openssh-client")
if [ ${#miss[@]} -gt 0 ]; then
  echo
  say "缺少依赖: ${miss[*]}"
  echo "    apt-get install -y python3-yaml openssh-client sshpass"
  echo "    （sshpass 只在清单里用密码认证时需要, 建议改用 SSH 密钥）"
fi

if [ ! -f "$PROJECT_DIR/inventory/servers.yml" ]; then
  cp "$PROJECT_DIR/inventory/servers.example.yml" "$PROJECT_DIR/inventory/servers.yml"
  chmod 600 "$PROJECT_DIR/inventory/servers.yml"
  ok "已生成清单模板: $PROJECT_DIR/inventory/servers.yml (权限 600)"
fi

echo
echo "下一步："
echo "    vi $PROJECT_DIR/inventory/servers.yml        # 填你的机器"
echo "    cd $PROJECT_DIR"
echo "    python3 orchestrator/fleet.py detect --dry-run   # 先确认命令拼对"
echo "    python3 orchestrator/fleet.py detect"
echo
echo "先跑 tcpfit detect 看机器画像, 再决定参数."
