#!/usr/bin/env bash
# tcpfit — 单机 TCP 调优代理
#
# 国际线路调优使用 Bash；优化线路调优双端模式另需 Python 3、curl 和 iperf3。
# 所有"该设多少"的判断都由实测或机器规格推导, 不使用抄来的固定值.
#
# 菜单 1：国际线路调优；菜单 2：优化线路调优。
#
# 用法:
#   tcpfit.sh                               交互式菜单（不带参数即可, 推荐）
#   tcpfit.sh return [选项]                  优化线路调优（家宽主动接入，仅服务器调参）
#   tcpfit.sh return-recover                 重试未完成的优化线路调优任务恢复
#   tcpfit.sh detect                        输出机器画像
#   tcpfit.sh probe  --peer HOST            探测可用带宽(虚拟网卡读不到标称值时用)
#   tcpfit.sh tune   [选项]                 应用基础调优
#   tcpfit.sh buffer --max-mb N [--default-mb N]  自定义 TCP 缓冲区（MiB）
#   tcpfit.sh sweep  --peer HOST [选项]     实测限速器拐点 (-4/-6 指定协议族, 默认 -4)
#   tcpfit.sh shape  --rate N | --off       应用/移除出向整形
#   tcpfit.sh harden --swap 2G              加 swap（小内存机防止进程被杀）
#   tcpfit.sh verify [--peer HOST]          验证当前状态
#   tcpfit.sh status                        显示当前配置
#   tcpfit.sh rollback                      回滚到出厂（= archive restore 0000）
#   tcpfit.sh archive list                  列出所有调优存档
#   tcpfit.sh archive save [名字]           把当前状态存成新存档
#   tcpfit.sh archive restore <序号|名字>   回滚到指定存档
#   tcpfit.sh archive rename <序号> <名字>  改名（0000 不可改）
#   tcpfit.sh archive delete <序号>         删除（0000 不可删）
#   tcpfit.sh uninstall [--keep-archives]   卸载：回滚 + 删配置 + 删自己
#
# 运行计数: 启动时会向 tcpfit.spacevps.cc 发一次匿名计数请求（纯计数, 不含任何
#           机器标识, 只带版本号）, 用于显示"今天多少次 / 累计多少次".
#           关掉:  TCPFIT_NO_TELEMETRY=1   或   touch /var/lib/tcpfit/no-telemetry
#
# 退出码: 0 成功 / 1 参数或环境错误 / 2 实测失败

set -uo pipefail
umask 022   # 固定权限: 生成的脚本和配置不能因为宽松 umask 变成他人可写

VERSION="0.18.3"
REPO="P0me1oo/tcpfit-x"
SOURCE_FILE="${BASH_SOURCE[0]}"
STATE_DIR="/var/lib/tcpfit"
SYSCTL_FILE="/etc/sysctl.d/99-tcpfit.conf"
QDISC_SCRIPT="/usr/local/sbin/tcpfit-qdisc.sh"
QDISC_UNIT="/etc/systemd/system/tcpfit-qdisc.service"
ROUTE_HOOK="/etc/networkd-dispatcher/routable.d/50-tcpfit-initcwnd"
BBR_MODULE_FILE="/etc/modules-load.d/tcpfit-bbr.conf"
INITCWND_MARKER="$STATE_DIR/initcwnd.owned"
SNAPSHOT="$STATE_DIR/pre-tune.snapshot"
FACTS="$STATE_DIR/facts"

# ── 输出 ────────────────────────────────────────────────────────────────────
# 配色对齐 x-ui, 用户在同一台机器上看到的风格一致
if [ -t 1 ]; then
  green=$'\033[0;32m'; red=$'\033[0;31m'; yellow=$'\033[0;33m'
  blue=$'\033[0;36m';  bold=$'\033[1m';   plain=$'\033[0m'
else
  green=''; red=''; yellow=''; blue=''; bold=''; plain=''
fi
_c(){ [ -t 1 ] && printf '\033[%sm%s\033[0m' "$1" "$2" || printf '%s' "$2"; }
info(){ printf '%s %s\n' "$(_c '0;36' '[*]')" "$*"; }
ok(){   printf '%s %s\n' "$(_c '0;32' '[+]')" "$*"; }
warn(){ printf '%s %s\n' "$(_c '0;33' '[!]')" "$*" >&2; }
die(){  printf '%s %s\n' "$(_c '0;31' '[x]')" "$*" >&2; exit "${2:-1}"; }

# 按显示宽度对齐：CJK 占 2 列, printf 的 %-Ns 按字节算会错位.
# 不能依赖 awk 的多字节支持 —— mawk(Debian 默认) 没有, 会把 3 字节的中文算成 3 个字符.
# 这里直接按 UTF-8 前导字节判断：ASCII=1列, 2字节序列=1列, 3字节及以上=2列, 续字节=0列.
_dispw(){
  printf '%s' "$1" | LC_ALL=C od -An -tu1 2>/dev/null | awk '
    {for(i=1;i<=NF;i++){b=$i
       if(b<128)            n++          # ASCII
       else if(b<192)       continue     # 续字节, 不计宽
       else if(b<224)       n++          # 2 字节序列(拉丁扩展等)
       else if(b==226){ nx=$(i+1); if(nx==148||nx==149){ n++; i+=2; continue } n+=2 }
       else                 n+=2         # 3 字节及以上(CJK、全角符号)
    }} END{print n+0}'
}
kv(){ local w; w=$(_dispw "$1"); printf '  %s%*s %s\n' "$1" $(( 20 - w )) "" "$2"; }
# 把字符串按「显示宽度」补齐到 N 列, 供手工排表用
_pad(){  local w; w=$(_dispw "$1"); printf '%s%*s' "$1" $(( $2 - w )) ""; }
_rpad(){ local w; w=$(_dispw "$1"); printf '%*s%s' $(( $2 - w )) "" "$1"; }
# 「确认」和「结果」里的两列排版
_conf(){ printf '      %s %s\n' "$(_pad "$1" 14)" "$2"; }

# 同时跑两个实例会同时抢 qdisc、快照和 sysctl. 用文件锁串行化.
LOCK_FILE="/var/lock/tcpfit.lock"
LOCK_HELD=0
take_lock(){
  # 同一流程会多次调用 tune/shape；保留同一个描述符，避免重开时短暂释放锁。
  [ "$LOCK_HELD" = 1 ] && return 0
  command -v flock >/dev/null || die "缺少 flock，请先安装 util-linux；未启动调优"
  mkdir -p "$(dirname "$LOCK_FILE")" || die "无法创建任务锁目录"
  exec 9>>"$LOCK_FILE" || die "无法打开任务锁: $LOCK_FILE"
  flock -n 9 || die "已有 tcpfit 任务正在运行，请等待其结束后重试（锁: $LOCK_FILE）"
  [ "${1:-}" = recovery ] || [ ! -f "$STATE_DIR/return-pending.json" ] || die "存在尚未恢复完成的优化线路调优任务，请先执行 tcpfit return-recover"
  LOCK_HELD=1
}

need_root(){ [ "$(id -u)" = 0 ] || die "需要 root 权限"; }

# 转圈. 长操作(iperf3 一跑十几秒)不给反馈的话用户会以为卡死了.
# 非交互环境(管道/日志)不画, 避免把日志刷满控制字符.
SPIN_FRAMES='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
spin_wait(){   # spin_wait <pid> <描述>
  local pid="$1" msg="$2" i=0
  if [ ! -t 2 ]; then wait "$pid" 2>/dev/null; return $?; fi
  while kill -0 "$pid" 2>/dev/null; do
    printf '\r  \033[0;36m%s\033[0m %s' "${SPIN_FRAMES:$((i++%10)):1}" "$msg" >&2
    sleep 0.12
  done
  printf '\r\033[K' >&2
  wait "$pid" 2>/dev/null
}
# ── 流量计量 ──────────────────────────────────────────────────────────────
# 读网卡字节计数器. 比按速率估算准 —— 它把重传、协议开销、握手全算进去了.
TRAFFIC_RX0=""; TRAFFIC_TX0=""
traffic_mark(){
  local i; i=$(detect_iface)
  TRAFFIC_RX0=$(cat "/sys/class/net/$i/statistics/rx_bytes" 2>/dev/null || echo 0)
  TRAFFIC_TX0=$(cat "/sys/class/net/$i/statistics/tx_bytes" 2>/dev/null || echo 0)
}
traffic_report(){
  [ -n "$TRAFFIC_TX0" ] || return 0
  local i rx tx drx dtx; i=$(detect_iface)
  rx=$(cat "/sys/class/net/$i/statistics/rx_bytes" 2>/dev/null || echo 0)
  tx=$(cat "/sys/class/net/$i/statistics/tx_bytes" 2>/dev/null || echo 0)
  drx=$(( rx - TRAFFIC_RX0 )); dtx=$(( tx - TRAFFIC_TX0 ))
  [ "$drx" -lt 0 ] && drx=0; [ "$dtx" -lt 0 ] && dtx=0
  echo
  printf '  %s本次测试消耗流量%s\n' "$bold" "$plain"
  rule
  awk -v tx="$dtx" -v rx="$drx" '
    function h(b){ if(b>=1073741824) return sprintf("%.2f GB", b/1073741824); return sprintf("%.0f MB", b/1048576) }
    BEGIN{
      printf "  %-16s %s\n","出向 (上传)", h(tx)
      printf "  %-16s %s\n","入向 (下载)", h(rx)
      printf "  %-16s %s\n","双向合计", h(tx+rx)
    }'
  rule
}

rule(){ printf '  \033[2m%s\033[0m\n' "────────────────────────────────────────────────"; }
step(){ printf '\n  \033[1;36m▸ %s\033[0m\n' "$*"; }

# 用 bash <(curl ...) 一条命令跑时, $0 是临时 fd, 脚本一退出就没了.
# 这里把自己装到系统里, 以后想回滚/查状态还能找到.
# 测速走哪个协议族. 默认 IPv4 —— 双栈机器上 v4 和 v6 到同一个对端的延迟可能差很多,
# 实测见过同城对端 v4 0.8ms / v6 93ms, 按 v6 的 RTT 选对端会把最好的那个判成"太远".
# 更麻烦的是 ping 和 iperf3 各自独立解析, 可能一个走 v4 一个走 v6 ——
# 那样挑选依据和实际测量根本不是同一条链路.
IP_FAMILY="${IP_FAMILY:--4}"

# 按当前协议族把主机名解析成字面地址. bash 的 /dev/tcp 没法指定协议族,
# 只能先解析好再连. 注意 v6 字面量不能加方括号, bash 认不了.
#
# -6 那支必须滤掉 ::ffff: 开头的 v4 映射地址 —— getent ahostsv6 对只有 A 记录的
# 主机也会返回结果(如 ::ffff:20.205.243.166), 而 iperf3 -6 连这种地址还会成功.
# 不滤的话: 用户选了 v6, 整个测试悄悄跑在 IPv4 上, 一句提示都没有.
resolve_ip(){   # resolve_ip <主机名>
  case "$IP_FAMILY" in
    -6) getent ahostsv6 "$1" 2>/dev/null | awk '/STREAM/ && $1 !~ /^::ffff:/ {print $1; exit}' ;;
    *)  getent ahostsv4 "$1" 2>/dev/null | awk '/STREAM/{print $1; exit}' ;;
  esac
}
# 端口探测: 解析不出对应协议族的地址就直接算不可达
probe_port(){   # probe_port <主机> <端口> [超时秒]
  local ip; ip=$(resolve_ip "$1"); [ -n "$ip" ] || return 1
  timeout "${3:-6}" bash -c "cat < /dev/null > /dev/tcp/${ip}/${2}" 2>/dev/null
}

# 对端 iperf3 实例的端口范围. Leaseweb/OVH 开 5201-5210, Clouvider 开 5200-5209 ——
# 所以 5200 也得在表里. 放末尾: 放开头会让 16 个 Leaseweb/OVH 节点每次都先白撞一下.
PORT_POOL="5201 5202 5203 5204 5205 5206 5207 5208 5209 5210 5200"

# 把首选端口排到表最前面, 其余保持原序. run_iperf 和选对端共用一份顺序.
port_order(){   # port_order <首选端口>
  local p out="$1"
  for p in $PORT_POOL; do [ "$p" = "$1" ] || out="$out $p"; done
  echo "$out"
}

# 选对端时的预检端口. 只探 5201 会出大事 —— 5201 是 iperf3 默认端口, 有机房
# 专门封它防测速滥用. 实测过一台客户机器(Debian 13, 依赖齐全, 无本地防火墙):
# 出站 5201 被单独封死(对 6 个不同目标 0/5), 而 5200/5202/5210/5211/6201 全是 5/5,
# 结果 18 个节点全被判成 "port closed", 工具完全不可用, 最后那句
# "公共测速服务器暂时都不可用" 还把责任推给了完全无辜的对端.
PROBE_PORTS="5201 5202 5203 5200"
PROBE_HIT=""        # 上一个探通的端口. 出站封锁对所有节点一致, 记住能省掉 17 次重复失败
PROBE_PORT_OK=""    # probe_peer_port 的结果

# 不能用 $(...) 取结果 —— 命令替换是子 shell, PROBE_HIT 记不住, 缓存就失效了.
probe_peer_port(){  # probe_peer_port <主机>  -> 成功则 PROBE_PORT_OK=端口
  local try seen=""
  PROBE_PORT_OK=""
  for try in $PROBE_HIT $PROBE_PORTS; do
    case " $seen " in *" $try "*) continue ;; esac      # PROBE_HIT 可能和表里重复
    seen="$seen $try"
    probe_port "$1" "$try" 4 || continue
    PROBE_HIT="$try"; PROBE_PORT_OK="$try"; return 0
  done
  return 1
}

# ⚠ 本文件顶部是 `set -uo pipefail`, 所以【绝对不能写 `命令 | grep -q 模式`】.
# grep -q 一匹配就立刻退出并关掉管道, 写端还没写完就吃 SIGPIPE 死掉,
# pipefail 把 141 当成整条管道的返回值 —— 于是"匹配到了"被读成"没匹配到".
# 只要匹配点之后还有内容要写就会触发. 2026-08-11 客户机实测:
#   ip -4 addr show scope global | grep -q 'inet '   →  292/300 返回 141
# 那台装了 docker, eth0 后面还跟着 docker0; 没有 docker 的机器 eth0 就是最后一个,
# ip 写完了 grep 才退出, 于是永远不复现 —— 同款机器一台好一台坏, 全卡在这里.
# 统一改成命令替换 + case: 读到 EOF, 写端永远不会被打断.
has_str(){  case "$1" in *"$2"*) return 0 ;; *) return 1 ;; esac; }
starts_with(){ case "$1" in "$2"*) return 0 ;; *) return 1 ;; esac; }   # 等价 grep -q '^…'
# 等价 grep -qw: 匹配处前后都不能是单词字符. 不能简单按空格切 ——
# 实测 "reno,cubic,bbr" 和 tab 分隔的列表 grep -qw 都算命中, 按空格切会漏判.
has_word(){ case "$1" in
    "$2"|"$2"[!A-Za-z0-9_]*|*[!A-Za-z0-9_]"$2"|*[!A-Za-z0-9_]"$2"[!A-Za-z0-9_]*) return 0 ;;
    *) return 1 ;; esac; }
# 等价 grep -q . : 只要有【任意非换行字符】就算有内容. 用 [![:space:]] 会把
# 纯空格的输出判成空, 和原来的行为不一致.
not_blank(){ case "$1" in *[!$'\n']*) return 0 ;; *) return 1 ;; esac; }
# 取第一行, 空则给默认值. 不用 `| head -1 || 默认值` —— head 读够一行就退出,
# 写端(grep)还有匹配要输出时被 SIGPIPE 打死, pipefail 返回 141, `||` 于是误触发,
# 结果是「真值 + 默认值」一起打出来.
first_or(){ local s="${1%%$'\n'*}"; if [ -n "$s" ]; then printf '%s' "$s"; else printf '%s' "$2"; fi; }

# tc 显示速率时【只在除得尽的时候才换单位】. 2026-08-11 两台机器实测 (iproute2 5.15):
#   999→999Mbit  1000→1Gbit  1001→1001Mbit  1500→1500Mbit
#   2000→2Gbit   2500→2500Mbit  3000→3Gbit  5000→5Gbit  9171→9171Mbit  10000→10Gbit
# 所以 "2.5Gbit"/"9.171Gbit" 这种带小数的显示【不会出现】——
# 曾经的注释和一份 review 都这么写过, 是错的, 别再照抄.
# 真正会踩坑的是【整千值】: 1000/2000/3000/5000/10000, 它们显示成 "NGbit".
# 所有读取整形值的地方都必须走这个函数, 不许各写各的正则. 早期 status / verify /
# banner / 向导结果四处各写死 'rate [0-9]+[MKG]bit' + "${shaper%Mbit}", 后果是
# "1Gbit" 剥不掉 "Gbit" → 当成非数字 → 达成率判断被跳过, 还反过来提示
# "这台没有应用整形"（`tcpfit shape --rate 1000` 对 1G 口是最自然的手输值）.
# 小数分支保留是为了防御: 换个 tc 版本万一真打出小数, 这里也算得对.
# 返回 Mbit 数字; 没有整形时返回空并且退出码非 0.
tc_rate_mbit(){   # tc_rate_mbit "<tc class show 的输出>"
  local r
  r=$(grep -oE 'rate [0-9.]+[KMGTkmgt]?bit' <<<"${1:-}")
  r=${r%%$'\n'*}          # 只要第一条; 不用 `| head -1`, 见上面 SIGPIPE 那段注释
  r=${r#rate }
  [ -n "$r" ] || return 1
  awk -v s="$r" 'BEGIN{
    u = s; sub(/^[0-9.]+/, "", u); sub(/bit$/, "", u)
    v = s + 0
    if      (u == "K" || u == "k") v = v/1000
    else if (u == "G" || u == "g") v = v*1000
    else if (u == "T" || u == "t") v = v*1000000
    else if (u == "")              v = v/1000000
    if (v == int(v)) printf "%d", v; else printf "%g", v }'
}

# 本机有没有可用的 IPv4 出网能力. 纯 v6 机器要自动走 v6, 不能傻等 v4 超时.
have_ipv4(){
  local rt ad
  rt=$(ip -4 route show default 2>/dev/null)
  ad=$(ip -4 addr show scope global 2>/dev/null)
  not_blank "$rt" && has_str "$ad" 'inet ' && return 0
  # 默认路由不一定叫 "default". 机器上跑着 VPN/透明代理(WireGuard、sing-box、Clash TUN)时,
  # 全局路由常被拆成 0.0.0.0/1 + 128.0.0.0/1, 或整个挪进策略路由的独立表 ——
  # 这两种情况 `route show default` 都是空的, 而机器的 v4 明明是通的.
  # `route get` 走内核真正的选路逻辑, 拆分路由和策略表都算数; 输出里有 src
  # 就说明既选得出出口、也有全局源地址, 一次覆盖原来那两个条件.
  has_str "$(ip -4 route get 1.1.1.1 2>/dev/null)" ' src '
}

# 本机有没有可用的 IPv6 出网能力. 光有地址不算 —— 很多机器配了 v6 地址但没路由.
have_ipv6(){
  local rt ad
  rt=$(ip -6 route show default 2>/dev/null)
  ad=$(ip -6 addr show scope global 2>/dev/null)
  not_blank "$rt" && has_str "$ad" 'inet6' && return 0
  has_str "$(ip -6 route get 2606:4700:4700::1111 2>/dev/null)" ' src '
}

PEER_PORT="${PEER_PORT:-5201}"   # 选定对端时确定的可用端口
WIZARD=0                         # 国际线路调优流程内为 1：子命令只输出执行日志, 收尾统一由 wizard 打印
# 装成不带扩展名的 tcpfit, 放 /usr/local/bin —— 用户敲 `tcpfit` 就能进菜单.
# 不用 /usr/local/sbin 是因为它不在普通用户的 PATH 里, 非 root 敲命令会「找不到命令」,
# 而不是看到「需要 root 权限」这个有用的提示.
SELF_PATH="/usr/local/bin/tcpfit"
LEGACY_SELF="/usr/local/sbin/tcpfit.sh"   # v0.3.1 及更早装在这里, 装新版时清掉
# 面向用户的提示一律用这个, 不能用 $0 ——
# bash <(curl ...) 跑时 $0 是 /dev/fd/63, 提示出来的命令用户根本没法执行
# 提示用户"下一步敲什么". 装好之后 tcpfit 在 PATH 里, 直接说命令名即可；
# 没装成（非 root / 没 curl）才退回完整路径. 绝不能用 $0 ——
# bash <(curl ...) 跑时 $0 是 /dev/fd/63, 提示出来的命令用户根本没法执行.
disp(){
  [ -x "$SELF_PATH" ] && { echo "tcpfit"; return; }
  case "$0" in /dev/fd/*|/proc/self/fd/*|bash|-bash) echo "$SELF_PATH" ;; *) echo "$0" ;; esac
}
# 装到系统里的那一份, 必须和「你刚跑的这一份」是同一个版本.
#
# 原先无条件拉 main：你按 v0.3.0 下载、校验、运行, 它转头把 main 装进
# /usr/local/sbin —— 之后每次敲 tcpfit.sh 跑的都是没校验过的代码,
# 固定版本的意义被完全抵消. （我自己踩过：推完新版去远端验证, 看到的还是旧菜单.)
#
# 为什么不能直接复制"正在运行的脚本"：bash <(curl ...) 时 $0 是 /dev/fd/63,
# 内容已被 bash 读走, 再 cat 只能读到 0 字节；curl | bash 时 $0 = bash, 根本不可读.
# 实测验证过这两种情况. 所以只能按版本号回拉, 并校验拉到的确实是同一版.
SELF_URL="https://raw.githubusercontent.com/${REPO}/v${VERSION}/tcpfit.sh"
self_install(){
  [ "$(id -u)" = 0 ] || return 0
  case "$0" in "$SELF_PATH") return 0 ;; esac      # 已经是装好的那份
  command -v curl >/dev/null || return 0
  curl -fsSL "$SELF_URL" -o "$SELF_PATH".tmp 2>/dev/null || { rm -f "$SELF_PATH".tmp; return 0; }
  # 校验版本一致. 开发期 main 领先 tag 时这里会失败, 跳过安装也是对的.
  if [ -s "$SELF_PATH".tmp ] && starts_with "$(head -1 "$SELF_PATH".tmp 2>/dev/null)" '#!' \
     && grep -q "^VERSION=\"$VERSION\"" "$SELF_PATH".tmp; then
    mv "$SELF_PATH".tmp "$SELF_PATH"; chmod +x "$SELF_PATH"
    rm -f "$LEGACY_SELF"                      # 清掉旧位置, 免得两份不同版本并存
    ok "Installed: run 'tcpfit' anytime"
  else
    rm -f "$SELF_PATH".tmp
  fi
}

# ── 从旧名字 nettune 迁移 ──────────────────────────────────────────────────
# 项目 v0.3.1 从 nettune 改名为 tcpfit. 老机器上所有产物的文件名都还是 nettune-*,
# 新脚本按新名字去找会一个都找不到 —— 最危险的是 take_snapshot 的保护：
# 它检查 $SYSCTL_FILE 是否存在, 改名后该变量指向新路径, 老文件在它眼里不存在,
# 于是把「已调优状态」当成出厂基线存进快照, rollback 从此永久错误且无任何报错.
# 所以必须先搬迁, 而不是假装老部署不存在.
migrate_legacy(){
  local old_state=/var/lib/nettune
  local old_sysctl=/etc/sysctl.d/99-nettune.conf
  local old_qdisc=/usr/local/sbin/nettune-qdisc.sh
  local old_unit=/etc/systemd/system/nettune-qdisc.service
  local old_hook=/etc/networkd-dispatcher/routable.d/50-nettune-initcwnd
  local old_mod=/etc/modules-load.d/nettune-bbr.conf
  local old_self=/usr/local/sbin/nettune.sh
  # 一个旧产物都没有 → 全新机器, 什么都不用做
  [ -e "$old_state" ] || [ -e "$old_sysctl" ] || [ -e "$old_unit" ] || return 0
  [ "$(id -u)" = 0 ] || return 0

  info "检测到旧版本(nettune)的部署, 正在迁移到新名字(tcpfit)…"
  local rate=""
  # 先把整形值抠出来, 后面用新名字重建；不能直接改文件名, unit 里的路径也要跟着变
  [ -f "$old_qdisc" ] && rate=$(grep -oE 'rate [0-9]+mbit' "$old_qdisc" | head -1 | grep -oE '[0-9]+')
  systemctl disable --now nettune-qdisc.service >/dev/null 2>&1
  rm -f "$old_unit" "$old_qdisc"; systemctl daemon-reload >/dev/null 2>&1

  # 逐文件搬, 不搬目录 —— mv -n 在目标目录已存在时会变成 STATE_DIR/nettune/,
  # 快照就找不到了; 而后面的 rm -rf 还可能把原数据删掉
  if [ -d "$old_state" ]; then
    mkdir -p "$STATE_DIR"
    for _f in "$old_state"/*; do
      [ -e "$_f" ] || continue
      [ -e "$STATE_DIR/$(basename "$_f")" ] || mv "$_f" "$STATE_DIR/"
    done
    rmdir "$old_state" 2>/dev/null || warn "旧目录 $old_state 非空, 已保留"
  fi
  [ -f "$old_sysctl" ] && mv -f "$old_sysctl" "$SYSCTL_FILE"
  [ -f "$old_hook" ]   && mv -f "$old_hook" "$ROUTE_HOOK"
  [ -f "$old_mod" ]    && mv -f "$old_mod" /etc/modules-load.d/tcpfit-bbr.conf
  rm -f "$old_self" "$LEGACY_SELF"

  if [ -n "$rate" ]; then
    write_qdisc "$rate" "$(detect_iface)"
    systemctl restart tcpfit-qdisc.service 2>/dev/null || "$QDISC_SCRIPT" >/dev/null 2>&1
    ok "迁移完成, 整形 ${rate}Mbit 已用新名字重建"
  else
    ok "迁移完成"
  fi
  info "快照保留在 $SNAPSHOT, rollback 仍然可用."
}

# ── 环境检测 ────────────────────────────────────────────────────────────────
# 默认路由网卡. 【不跟 $IP_FAMILY 走】—— 网卡是物理概念, 整形和 qdisc 打在同一张卡上,
# 选 v4 还是 v6 测速都是它. 只是纯 v6 机器的 v4 路由表是空的, 所以 v4 查不到时回退查 v6.
# (`ip route` 等价于 `ip -4 route`, 早期版本只写这一句, 纯 v6 机器直接
#  die "找不到默认路由网卡", 从来就没跑起来过.)
detect_iface(){
  local i
  i=$(ip -4 route show default 2>/dev/null | awk '{print $5; exit}')
  [ -n "$i" ] || i=$(ip -6 route show default 2>/dev/null | awk '{print $5; exit}')
  echo "$i"
}
# 网关【只取 v4】. 它唯一的用途是 `ip route replace default via $gw ...`(设 initcwnd),
# 那是 IPv4 路由表操作, 喂 v6 地址进去会直接报
# "Error: inet address is expected rather than 2a0f:...". 实测验证过.
# 纯 v6 机器上这里返回空, 调用方的 [ -n "$gw" ] 会跳过 initcwnd —— 安全降级.
detect_gw(){    ip -4 route show default 2>/dev/null | awk '{print $3; exit}'; }

# 只清理 tcpfit 自己写入的 initcwnd/initrwnd. 旧版本没有 ownership marker,
# 所以兼容两种证据: tcpfit 的持久化 hook, 或快照明确显示调优前没有这两个属性.
# 重建路由时沿用 `ip route show` 的全部 token, 只剔除窗口字段，避免丢掉
# metric/proto/src/onlink 等服务商下发的属性.
clear_owned_initcwnd(){
  local route routes before owned=0 skip=0 token
  local -a args clean
  INITCWND_CLEARED=0
  routes=$(ip -4 route show default 2>/dev/null)
  route=${routes%%$'\n'*}

  [ -f "$INITCWND_MARKER" ] && owned=1
  [ -f "$ROUTE_HOOK" ] && owned=1
  if [ "$owned" = 0 ] && [ -f "$SNAPSHOT" ]; then
    before=$(awk '/^# route: /{sub(/^# route: /, ""); print; exit}' "$SNAPSHOT")
    if [ -n "$before" ] && ! has_str "$before" ' initcwnd ' && \
       ! has_str "$before" ' initrwnd ' && \
       { has_str "$route" ' initcwnd 32' || has_str "$route" ' initrwnd 32'; }; then
      owned=1
    fi
  fi
  [ "$owned" = 1 ] || return 0
  INITCWND_CLEARED=1

  # 先移除持久化入口；即使运行时路由暂时改不了，重连/重启后也不会再写回 32.
  rm -f "$ROUTE_HOOK" "$INITCWND_MARKER"
  if ! has_str "$route" ' initcwnd ' && ! has_str "$route" ' initrwnd '; then
    return 0
  fi

  read -r -a args <<<"$route"
  for token in "${args[@]}"; do
    if [ "$skip" = 1 ]; then skip=0; continue; fi
    case "$token" in
      initcwnd|initrwnd) skip=1 ;;
      *) clean+=("$token") ;;
    esac
  done
  [ "${#clean[@]}" -gt 0 ] || return 1
  ip -4 route replace "${clean[@]}" 2>/dev/null
}

# 算 BDP 用的 RTT. 固定 150ms, 不再探测.  用 --rtt 可以覆盖.
#
# 为什么不测了 —— 旧做法是 ping 五个国内 DNS 取中位数, 三个问题让它没法用:
#
#  1. anycast 污染. 五个目标里腾讯/百度/CNNIC 三个是 anycast, 会命中就近节点.
#     本机(香港)实测: 2ms / 1ms / 1ms, 而真·国内是 138-145ms —— 中位数取出 2ms,
#     差 70 倍. 更糟的是 BDP 算小之后缓冲区落到 4MB 下限, 而 4MB 正好等于
#     Linux 出厂值, 等于"调了个寂寞", 还打印一份看着完全正常的推导过程.
#  2. 硬依赖 ping + ICMP. 精简镜像不带 iputils-ping, 有的机房挡 ICMP ——
#     两种情况都让 detect_rtt 返回空, 然后 die "无法确定 RTT, 请用 --rtt 指定",
#     而向导里根本没地方填这个参数, 报错把用户指向死路. 客户真踩过.
#  3. 就算测准了也没意义. "到中国的 RTT"不是一个数: 同一台机器同一时刻实测
#     移动 55ms / 联通 93ms / 电信 138ms / 上海电信 145ms, 差 2.6 倍,
#     再叠加晚高峰. 测出来的只是这个分布里随机的一个点.
#
# 默认按 150 ms 估算往返延迟，缓冲区从 1.5×BDP + 2 MiB 起步：
#     优化线 40-70ms / 香港普通线 145ms / 美西 160-180ms / 欧美 230-250ms /
#     晚高峰拥塞 300ms 都不会按某次不可靠的 ping 把缓冲区算得特别小.
# 这不是承诺单流全速覆盖到 300ms: tcp_adv_win_scale=1 会为协议和应用预留
# 一部分 socket 空间, 实际可通告窗口还受内核记账、路径和对端共同影响.
# 再提高默认估值收益有限, 而且
#     小内存机器会被内存试调上限截断（512 MiB 档为 24 MiB）;
#     大机器上则要多付 BBR 超发的账 —— 实测超配 215 倍时掉 22% 吞吐.
# 估低才是真危险: 估 40 时缓冲区会算得过小, 2G 口到美西只剩 941 Mbps(47%),
#     而且是硬天花板, 用户怎么测都上不去还查不出原因.
DEFAULT_RTT=150

detect_ram_mb(){ awk '/^MemTotal:/{printf "%d", $2/1024}' /proc/meminfo; }
detect_cores(){  nproc 2>/dev/null || echo 1; }

# 网卡标称速率. 虚拟网卡多半读不到, 返回空由调用方处理
detect_link_mbps(){
  local i="$1" s
  s=$(cat "/sys/class/net/$i/speed" 2>/dev/null)
  [[ "$s" =~ ^[0-9]+$ ]] && [ "$s" -gt 0 ] && echo "$s" || echo ""
}

cmd_detect(){
  local iface rtt ram cores link virt kern cc_avail queues
  iface=$(detect_iface); [ -n "$iface" ] || die "找不到默认路由网卡"
  rtt="$DEFAULT_RTT"; ram=$(detect_ram_mb); cores=$(detect_cores)
  link=$(detect_link_mbps "$iface")
  # systemd-detect-virt 在裸机上输出 none 但退出码为 1, 不能用 || 兜底
  virt=$(systemd-detect-virt 2>/dev/null); [ -n "$virt" ] || virt=unknown
  kern=$(uname -r)
  cc_avail=$(sysctl -n net.ipv4.tcp_available_congestion_control 2>/dev/null)
  queues=$(ls -d /sys/class/net/"$iface"/queues/rx-* 2>/dev/null | wc -l)

  echo "── Machine profile ──"
  kv "Interface"   "$iface"
  kv "Driver"      "$(ethtool -i "$iface" 2>/dev/null | awk -F': ' '/^driver/{print $2}')"
  kv "RX queues"   "$queues"
  kv "Link speed"  "${link:-n/a (virtual NIC)}"
  kv "Kernel"      "$kern"
  kv "Virt"        "$virt"
  kv "CPU cores"   "$cores"
  kv "Memory MB"   "$ram"
  kv "RTT (assumed)" "${rtt}ms  — 固定值, 用于估算 TCP 缓冲区; 要改用 tune --rtt"
  kv "CC available" "$cc_avail"
  kv "BBR"         "$(has_word "$cc_avail" bbr && echo 是 || (modprobe tcp_bbr 2>/dev/null && echo '是(需加载模块)' || echo 否))"

  mkdir -p "$STATE_DIR"
  cat > "$FACTS" <<EOF
IFACE=$iface
RTT_MS=${rtt:-0}
RAM_MB=$ram
CORES=$cores
LINK_MBPS=${link:-0}
KERNEL=$kern
VIRT=$virt
EOF
}

# 数值参数校验. 所有会改系统的子命令都必须在动手之前调它 ——
# 早期版本 shape --rate abc 会先存快照、再让 tc 报错, 留下垃圾状态.
is_posint(){   # is_posint <值> <最小> <最大>
  case "$1" in ''|*[!0-9]*) return 1 ;; esac
  [ "$1" -ge "$2" ] 2>/dev/null && [ "$1" -le "$3" ] 2>/dev/null
}

# ── 参数推导 ────────────────────────────────────────────────────────────────
# BDP(字节) = 带宽(Mbps) * 1e6 / 8 * RTT(s)
calc_bdp(){ awk -v b="$1" -v r="$2" 'BEGIN{printf "%d", b*1000000/8*(r/1000)}'; }

# tcp_mem(页). 内核在 pressure 阈值就开始缩窗, max 是硬顶.
# 小内存机器上把 max 设成内存的一半是 OOM 主因 —— 这里固定按 1/8 与 1/4 推导.
calc_tcp_mem(){
  local ram_mb="$1"
  awk -v m="$ram_mb" 'BEGIN{
    pg=m*1024/4;                      # 总内存页数
    low=int(pg/16); pres=int(pg/8); max=int(pg/4);
    if(low<4096) low=4096; if(pres<8192) pres=8192; if(max<16384) max=16384;
    printf "%d %d %d", low, pres, max
  }'
}

# 基础调优默认初始缓冲区上限 = 1.5 × BDP + 2 MiB，再受本机内存范围约束。
# 倍率用于初始估算，固定余量为窗口和内核记账留出空间；最终是否保留由实测决定。
# 优化线路调优由协调模块改用 1.5×BDP 初值和 2.5×BDP 试调上限，无 MiB 下限。
#
# 常规上限为内存的 1/32，绝对上限 256 MiB。512 MiB 档允许试到收发各
# 24 MiB；MemTotal 至少 448 MiB 即归入此档，容纳内核保留内存造成的差额。
# 更小的机器仍按原比例限制，超过此档后上限不下降；tcp_mem 总预算保持不变。
#
# 上述内存范围用于普通基础调优；优化线路调优按 BDP 限制缓冲区，
# 并用实际接收速度和重传决定是否保留候选。
calc_buf_limit(){   # calc_buf_limit <内存MiB>
  awk -v m="$1" 'BEGIN{
    cap = m*32768
    if(m >= 448 && cap < 25165824) cap = 25165824
    if(cap > 268435456) cap = 268435456
    if(cap < 4194304) cap = 4194304
    printf "%d", cap
  }'
}

calc_buf_max(){   # calc_buf_max <BDP字节> <内存MB>
  local limit; limit=$(calc_buf_limit "$2")
  awk -v b="$1" -v cap="$limit" 'BEGIN{
    v   = int(b*1.5 + 2097152)
    if(v > cap) v = cap
    if(v < 4194304) v = 4194304              # 下限 4MB, 低于此连百兆都跑不满
    printf "%d", v
  }'
}

# 显示最终上限来自公式、内存限制还是最小值。
buf_max_reason(){   # buf_max_reason <BDP字节> <内存MB> <算出的buf_max>
  local limit; limit=$(calc_buf_limit "$2")
  awk -v b="$1" -v cap="$limit" -v v="$3" 'BEGIN{
    target = int(b*1.5 + 2097152)
    if(v <= 4194304 && target < 4194304) { print "下限 4 MiB"; exit }
    if(v >= cap && target > cap)         { printf "受本机内存试调上限约束"; exit }
    print "1.5 × BDP + 2 MiB 余量"
  }'
}

# 整形安全余量：按标称带宽分 5 档给固定值.
# 不用百分比是因为百分比在两端都别扭 —— 100M 机器 3% 才 3Mbit 太小,
# 2G 机器 3% 就是 60Mbit 太浪费. 分档更贴合实际.
# 余量的意义：sweep 是在某个时刻测的, 晚高峰线路会变差, 留一点缓冲避免那时暴丢包.
# 安全余量. 现役档位换算成比例大约都是 2-5%, 所以小带宽也按这个比例给,
# 不能沿用"≤100M 一律 5"—— 15 Mbps 的线上 5 就是 33% 的容量, 实测干净区
# 上限 15 时会被整形到 10, 白丢三分之一.
calc_margin(){
  local bw="$1"
  if   [ "$bw" -le 30 ]   2>/dev/null; then echo 1      # ≤30M    5 就是三分之一, 只能给 1
  elif [ "$bw" -le 60 ]   2>/dev/null; then echo 2      # 31-60M
  elif [ "$bw" -le 100 ]  2>/dev/null; then echo 5      # 61-100M  原档位
  elif [ "$bw" -le 300 ]  2>/dev/null; then echo 10     # 101-300M
  elif [ "$bw" -le 600 ]  2>/dev/null; then echo 15     # 301-600M  最常见档位
  elif [ "$bw" -le 1000 ] 2>/dev/null; then echo 25     # 601-1000M
  else                                        echo 40   # >1G      大带宽波动也大
  fi
}

# HTB 令牌桶按 4ms 的线速数据量计算, 小带宽保留原来的 32k 下限.
# 固定 32k 在 2G/7G 下仍能工作, 但四轮隔离测试显示 2G 接收吞吐低约 0.24%.
# 只放大 burst 就能拿回这部分; 去掉 quantum / fq 队列参数没有额外收益,
# 还会让高带宽 HTB 报 quantum 过大的警告, 所以其余参数保持不变.
calc_burst(){   # calc_burst <rate_mbit> -> bytes
  awk -v r="$1" 'BEGIN{v=r*500; if(v<32768)v=32768; printf "%d",v}'
}

# 预估整个调优流程会跑掉多少流量. sweep 是大头 ——
# 档数随带宽线性增长, 每档还要按该速率跑满 12 秒, 千兆机器能跑掉几十 GB.
# 有流量配额的用户必须提前知道.
# 粗扫步长随带宽放大. 固定 20 时 2Gbps 机器要扫 40 档、跑掉 137GB ——
# 精度靠后面的细扫补, 粗扫没必要那么密.
calc_step(){ awk -v b="$1" 'BEGIN{s=int(b/30/10+0.5)*10; if(s<20)s=20; printf "%d", s}'; }

estimate_traffic_gb(){
  local st; st=$(calc_step "$1")
  awk -v b="$1" -v st="$st" 'BEGIN{
    steps = int(b*0.4/st) + 1            # 粗扫档数 = (1.2b-0.8b)/步长
    mb  = b*10/8                         # probe   4 流 10 秒
    mb += b*0.4                          # 路径验证 40% 速率 8 秒
    mb += (steps+3) * b*12/8             # 粗扫 + 细扫 3 档, 每档 12 秒
    mb += b*10/8*2                       # verify 单流 + 4 流各 10 秒
    printf "%.1f", mb/1024
  }'
}

# 缓冲区默认值（起点）决定爬坡快慢, 但每 socket 都吃这么多额度.
#   proxy 角色并发上百条连接 → 保守, 1MB
#   bulk  角色只有少数大流   → 激进, 可到 BDP
calc_buf_default(){
  local role="$1" bdp="$2"
  case "$role" in
    proxy) echo 1048576 ;;
    bulk)  awk -v b="$bdp" 'BEGIN{v=b; if(v<1048576)v=1048576; if(v>8388608)v=8388608; printf "%d", v}' ;;
    *)     echo 2097152 ;;
  esac
}

# 基础调优和优化线路调优共用推导入口，各自选用初值公式；默认值不能超过上限。
calc_buffer_profile(){   # <角色> <带宽Mbps> <RTT毫秒> <内存MB>
  local bdp maximum initial limit
  bdp=$(calc_bdp "$2" "$3")
  maximum=$(calc_buf_max "$bdp" "$4")
  initial=$(calc_buf_default "$1" "$bdp")
  [ "$initial" -le "$maximum" ] || initial="$maximum"
  limit=$(calc_buf_limit "$4")
  printf '%s %s %s %s\n' "$bdp" "$maximum" "$initial" "$limit"
}

BUFFER_KEYS="net.core.rmem_max net.core.wmem_max net.core.rmem_default net.core.wmem_default net.ipv4.tcp_rmem net.ipv4.tcp_wmem"

# 先完整读取并校验，避免缺少某个参数时已经改动了其他参数。
read_buffer_config(){
  local key raw count number values=() normalized=()
  for key in $BUFFER_KEYS; do
    raw=$(sysctl -n "$key" 2>/dev/null) || { warn "无法读取缓冲区参数: $key"; return 1; }
    read -r -a values <<<"$raw"
    count=1
    case "$key" in net.ipv4.tcp_rmem|net.ipv4.tcp_wmem) count=3 ;; esac
    [ "${#values[@]}" = "$count" ] || { warn "缓冲区参数不完整: $key"; return 1; }
    normalized=()
    for number in "${values[@]}"; do
      is_posint "$number" 1 2147483647 || { warn "缓冲区参数不是有效整数: $key"; return 1; }
      normalized+=("$((10#$number))")
    done
    printf '%s=%s\n' "$key" "${normalized[*]}"
  done
}

format_mib(){
  awk -v bytes="$1" 'BEGIN{
    if(bytes<1024){printf "%.0f 字节",bytes; exit}
    if(bytes<1048576){printf "%.2f KiB",bytes/1024; exit}
    v=bytes/1048576
    if(v==int(v))printf "%.0f MiB",v; else printf "%.2f MiB",v
  }'
}

# 菜单读取内核实际生效的 TCP 上限；读取失败时显示未知，不借用另一方向的值。
read_tcp_buffer_max(){
  local raw values=()
  raw=$(sysctl -n "$1" 2>/dev/null) || raw=""
  read -r -a values <<<"$raw"
  if [ "${#values[@]}" = 3 ] && is_posint "${values[2]}" 1 2147483647; then
    format_mib "${values[2]}"
  else
    printf '?'
  fi
}

# 只更新六项缓冲区参数。子 shell 独立管理中断与失败恢复，不覆盖调用者的 trap。
apply_buffer_config(){ (
  local maximum="$1" initial="${2:-}" previous key value chosen minimum actual
  local keys=() old=() next=() parts=() index candidate="" backup="" touched=0 persisted=0 committed=0 restore_log_fd
  is_posint "$maximum" 1 2147483647 || { warn "缓冲区上限必须在 1-2147483647 字节之间"; return 1; }
  maximum=$((10#$maximum))
  if [ -n "$initial" ]; then
    is_posint "$initial" 1 "$maximum" || { warn "缓冲区默认值不能超过上限"; return 1; }
    initial=$((10#$initial))
  fi
  previous=$(read_buffer_config) || return 1
  while IFS='=' read -r key value; do
    keys+=("$key"); old+=("$value")
    case "$key" in
      net.core.rmem_max|net.core.wmem_max) chosen="$maximum" ;;
      net.core.rmem_default|net.core.wmem_default)
        chosen="${initial:-$value}"
        [ "$chosen" -le "$maximum" ] || chosen="$maximum" ;;
      *)
        read -r -a parts <<<"$value"
        minimum="${parts[0]}"; chosen="${initial:-${parts[1]}}"
        [ "$chosen" -le "$maximum" ] || chosen="$maximum"
        [ "$minimum" -le "$chosen" ] || { warn "$key 的最小值超过请求的默认值或上限，未修改"; return 1; }
        chosen="$minimum $chosen $maximum" ;;
    esac
    next+=("$chosen")
  done <<<"$previous"
  if [ -L "$SYSCTL_FILE" ] || { [ -e "$SYSCTL_FILE" ] && [ ! -f "$SYSCTL_FILE" ]; }; then
    warn "缓冲区配置需要普通文件: $SYSCTL_FILE"; return 1
  fi
  exec {restore_log_fd}>&2

  buffer_cleanup(){
    local rc=$? restored=1 i current
    trap - EXIT HUP INT TERM
    if [ "$touched" = 1 ] && [ "$committed" = 0 ]; then
      for i in "${!keys[@]}"; do
        current=$(sysctl -n "${keys[i]}" 2>/dev/null | awk '{$1=$1;print}')
        [ "$current" = "${old[i]}" ] || sysctl -qw "${keys[i]}=${old[i]}" 2>/dev/null || restored=0
      done
      current=$(read_buffer_config) || restored=0
      [ "$current" = "$previous" ] || restored=0
      if [ "$persisted" = 1 ]; then
        if [ -n "$backup" ]; then
          if ! mv -f -- "$backup" "$SYSCTL_FILE"; then
            restored=0
            warn "原配置副本保留在: $backup" 2>&"$restore_log_fd"
            backup=""
          fi
        else rm -f -- "$SYSCTL_FILE" || restored=0; fi
      fi
      if [ "$restored" = 1 ]; then warn "本次缓冲区修改未完成，已恢复修改前的值" 2>&"$restore_log_fd"
      else warn "缓冲区恢复未完成，请按修改前存档恢复并检查系统报错" 2>&"$restore_log_fd"; fi
      [ "$rc" != 0 ] || rc=1
    fi
    [ -z "$candidate" ] || rm -f -- "$candidate"
    [ -z "$backup" ] || rm -f -- "$backup"
    exit "$rc"
  }
  trap buffer_cleanup EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  candidate=$(mktemp "${SYSCTL_FILE}.buffer.XXXXXX") || return 1
  if [ -f "$SYSCTL_FILE" ]; then
    backup=$(mktemp "${SYSCTL_FILE}.buffer-backup.XXXXXX") || return 1
    cp -p -- "$SYSCTL_FILE" "$backup" || return 1
    # 保留其他参数、注释和文件权限，移除缓冲区旧值及其重复项。
    cp -p -- "$SYSCTL_FILE" "$candidate" || return 1
    awk -v names="$BUFFER_KEYS" '
      BEGIN{n=split(names,a," "); for(i=1;i<=n;i++) keys[a[i]]=1}
      {key=$0; sub(/=.*/,"",key); gsub(/[[:space:]]/,"",key); sub(/^-/,"",key); gsub(/\//,".",key)
       if(!(key in keys) && $0 !~ /^# TCP 缓冲区由 tcpfit buffer 设置/ && $0 !~ /^# 缓冲区：上限=/) print}
    ' "$SYSCTL_FILE" > "$candidate" || return 1
  else
    printf '# 由 tcpfit v%s 生成\n' "$VERSION" > "$candidate" || return 1
    chmod 644 "$candidate" || return 1
  fi
  {
    printf '\n# TCP 缓冲区由 tcpfit buffer 设置，单位：字节\n'
    for index in "${!keys[@]}"; do printf '%s = %s\n' "${keys[index]}" "${next[index]}"; done
  } >> "$candidate" || return 1

  touched=1
  for index in "${!keys[@]}"; do
    [ "${old[index]}" = "${next[index]}" ] && continue
    sysctl -qw "${keys[index]}=${next[index]}" 2>/dev/null || {
      warn "内核拒绝缓冲区参数: ${keys[index]}=${next[index]}"; return 1;
    }
  done
  actual=$(read_buffer_config) || return 1
  local expected
  expected=$(for index in "${!keys[@]}"; do printf '%s=%s\n' "${keys[index]}" "${next[index]}"; done)
  [ "$actual" = "$expected" ] || { warn "缓冲区实际值与请求值不一致，未保留"; return 1; }
  persisted=1
  mv -f -- "$candidate" "$SYSCTL_FILE" || { warn "缓冲区配置保存失败"; return 1; }
  committed=1
); }

cmd_buffer(){
  local maximum="" initial=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --max-mb|--default-mb)
        [ $# -ge 2 ] && [ -n "$2" ] || die "$1 缺少数值"
        case "$1" in --max-mb) maximum="$2" ;; --default-mb) initial="$2" ;; esac
        shift 2 ;;
      -h|--help)
        printf '%s\n' '用法: tcpfit buffer --max-mb N [--default-mb N]' \
          '单位为 MiB（1 MiB = 1048576 字节），N 为 1-1024 的整数。' \
          '统一设置 TCP 收发缓冲区上限；默认值可另行指定，省略时保留并限制在新上限内。' \
          '立即生效并持久化，修改前后自动保存存档。'
        return 0 ;;
      *) die "未知缓冲区参数: $1" ;;
    esac
  done
  is_posint "$maximum" 1 1024 || die "--max-mb 必须是 1-1024 的整数（MiB）"
  maximum=$((10#$maximum))
  if [ -n "$initial" ]; then
    is_posint "$initial" 1 "$maximum" || die "--default-mb 必须为正整数且不能超过 --max-mb"
    initial=$((10#$initial * 1048576))
  fi
  need_root
  take_lock
  local before key value values=()
  local ARCH_MODE=buffer ARCH_RETURN_SNAPSHOT="" ARCH_RUN="" ARCH_ROLE="" ARCH_BW="" ARCH_RTT="" ARCH_PEER=""
  before=$(read_buffer_config) || return 1
  take_snapshot || return 1
  archive_save "before-buffer-${maximum}MiB" || { warn "修改前存档失败，未调整缓冲区"; return 1; }
  while IFS='=' read -r key value; do
    read -r -a values <<<"$value"
    case "$key" in
      net.ipv4.tcp_rmem) info "TCP 接收缓冲区上限：$(format_mib "${values[2]}") → ${maximum} MiB" ;;
      net.ipv4.tcp_wmem) info "TCP 发送缓冲区上限：$(format_mib "${values[2]}") → ${maximum} MiB" ;;
    esac
  done <<<"$before"
  apply_buffer_config "$((maximum * 1048576))" "$initial" || return 1
  ok "TCP 收发缓冲区上限已设为 ${maximum} MiB，已保存到 $SYSCTL_FILE"
  kv "tcp_rmem" "$(sysctl -n net.ipv4.tcp_rmem)"
  kv "tcp_wmem" "$(sysctl -n net.ipv4.tcp_wmem)"
  archive_save "buffer-${maximum}MiB" || warn "缓冲区已生效，但修改后存档未成功；修改前存档仍可恢复"
}

# 调优会动到的全部内核参数. 快照和回滚都以这份清单为准 ——
# 早期版本快照只记了 14 项而 tune 设了 31 项, 回滚后有 17 项在重启前仍是调优值.
# 加参数时必须同时加到这里, 否则那个参数就回滚不掉.
TUNED_KEYS="
  net.core.default_qdisc
  net.ipv4.tcp_congestion_control
  net.core.rmem_max
  net.core.wmem_max
  net.core.rmem_default
  net.core.wmem_default
  net.ipv4.tcp_rmem
  net.ipv4.tcp_wmem
  net.ipv4.tcp_mem
  net.ipv4.tcp_window_scaling
  net.ipv4.tcp_moderate_rcvbuf
  net.ipv4.tcp_adv_win_scale
  net.core.netdev_max_backlog
  net.core.netdev_budget
  net.core.netdev_budget_usecs
  net.core.optmem_max
  net.core.somaxconn
  net.ipv4.tcp_max_syn_backlog
  net.ipv4.tcp_slow_start_after_idle
  net.ipv4.tcp_no_metrics_save
  net.ipv4.tcp_mtu_probing
  net.ipv4.tcp_sack
  net.ipv4.tcp_dsack
  net.ipv4.tcp_timestamps
  net.ipv4.tcp_fastopen
  net.ipv4.tcp_syncookies
  net.ipv4.tcp_tw_reuse
  net.ipv4.tcp_fin_timeout
  net.ipv4.tcp_keepalive_time
  net.ipv4.ip_local_port_range
  vm.min_free_kbytes
  fs.file-max
  vm.swappiness
"

# ── 快照与回滚 ──────────────────────────────────────────────────────────────
# ── 包管理器锁诊断 ──────────────────────────────────────────────────────────
# 装 iperf3 失败最常见的原因不是"没网"或"包不存在", 而是 dpkg 锁被占:
# Ubuntu/Debian 首次开机 unattended-upgrades 会跑 5~30 分钟, 期间任何 apt 都装不上.
# 实测现场: 一台新开的机器 unattended-upgrades 占锁 20+ 分钟, tcpfit 静默降级成
# "只能基础调优", 用户以为是工具坏了.
apt_lock_holder(){   # 有锁则输出 "PID 进程名 已运行时长", 否则输出空
  local f pids p cmd age
  command -v fuser >/dev/null 2>&1 || return 0
  for f in /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock \
           /var/cache/apt/archives/lock /var/lib/apt/lists/lock; do
    [ -e "$f" ] || continue
    pids=$(fuser "$f" 2>/dev/null | tr -s ' ' '\n' | grep -x '[0-9]*')
    for p in $pids; do
      [ "$p" = "$$" ] && continue
      cmd=$(ps -o comm= -p "$p" 2>/dev/null)
      age=$(ps -o etime= -p "$p" 2>/dev/null | tr -d ' ')
      [ -n "$cmd" ] && { printf '%s %s %s' "$p" "$cmd" "${age:-?}"; return 0; }
    done
  done
  return 0
}

# 装包失败时讲清楚到底怎么回事, 而不是只说"没有 iperf3"
explain_pkg_failure(){   # explain_pkg_failure [包名]
  local pkg="${1:-iperf3}" holder up_s up_min
  holder=$(apt_lock_holder)
  up_s=$(awk '{printf "%d", $1}' /proc/uptime 2>/dev/null || echo 99999)
  up_min=$(( up_s / 60 ))

  echo >&2
  warn "${pkg} 没装上. 原因："
  if [ -n "$holder" ]; then
    set -- $holder
    warn "  包管理器正被占用 —— PID $1（$2）已运行 $3"
    case "$2" in
      unattended-upg*|apt-get|apt|aptd|dpkg|packagekitd)
        warn "  这是系统在自动装更新, 不是故障. 它跑完就能装了."
        ;;
      *) warn "  有另一个程序正在装东西, 等它结束." ;;
    esac
    echo >&2
    echo "    等它结束（通常 5~30 分钟，新机器可能更久）：" >&2
    echo "      while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do sleep 10; done; echo 可以了" >&2
    echo >&2
    echo "    想看它在干什么：" >&2
    echo "      journalctl -u unattended-upgrades -f      # 自动更新的日志" >&2
    echo "      ps -fp $1" >&2
  elif [ "$up_min" -lt 15 ]; then
    warn "  这台机器刚开机 ${up_min} 分钟."
    warn "  新装的 Ubuntu/Debian 开机后会自动跑一轮系统更新, 期间装不了别的包."
    echo >&2
    echo "    等几分钟再跑一次就行. 想确认是不是它：" >&2
    echo "      systemctl is-active unattended-upgrades apt-daily.service" >&2
  elif ! command -v apt-get >/dev/null && ! command -v dnf >/dev/null && \
       ! command -v yum >/dev/null && ! command -v apk >/dev/null; then
    warn "  认不出这台机器的包管理器."
    echo "    手动装好 iperf3 再重跑." >&2
  else
    warn "  包管理器没被占用, 装包命令本身失败了."
    echo >&2
    echo "    手动跑一次看真实报错：" >&2
    if command -v apt-get >/dev/null; then echo "      apt-get update && apt-get install -y ${pkg}" >&2
    elif command -v dnf >/dev/null;    then echo "      dnf install -y ${pkg}" >&2
    elif command -v yum >/dev/null;    then echo "      yum install -y ${pkg}" >&2
    elif command -v apk >/dev/null;    then echo "      apk add ${pkg}" >&2; fi
    echo "    常见原因: 没配软件源 / DNS 不通 / 磁盘满（df -h /）" >&2
  fi
  echo >&2
}

# ── ping 变种识别 ───────────────────────────────────────────────────────────
# `command -v ping` 存在【不代表能用】. 有服务商模板装的是 GNU inetutils 的 ping,
# 它不认 -4, 而 auto_pick_peer 是 `ping $IP_FAMILY ...`, IP_FAMILY 默认 -4 ——
# 18 个候选节点全部取不到 RTT, 最后报「公共测速服务器暂时都不可用」, 在甩锅给对端.
# 一台客户机实证: 把 -4 去掉后立刻选中最近的节点, RTT 2ms.
#
# 判定方式用"直接试"而不是解析 -V —— BusyBox 连 -V 都报 invalid option.
# 只看选项是否被拒, 不看 ping 本身通不通(有的机房挡 ICMP, 那是另一回事).
ping_supports_4(){
  local out
  out=$(ping -4 -c 1 -W 1 127.0.0.1 2>&1)
  case "$out" in
    *"invalid option"*|*"unrecognized option"*|*"illegal option"*|*"unknown option"*) return 1 ;;
  esac
  return 0
}

ping_variant(){
  command -v ping >/dev/null 2>&1 || { echo none; return; }
  ping_supports_4 && { echo iputils; return; }
  case "$(ping -V 2>&1 | head -1)" in
    *"GNU inetutils"*) echo inetutils ;;
    *BusyBox*|*busybox*) echo busybox ;;
    *) echo unknown ;;
  esac
}

# 装 iputils-ping. 成功返回 0.
install_iputils(){
  local holder; holder=$(apt_lock_holder)
  if [ -n "$holder" ]; then
    set -- $holder
    warn "包管理器正被 PID $1（$2, 已运行 $3）占用, 装不了."
    return 1
  fi
  if   command -v apt-get >/dev/null; then apt-get update -qq >/dev/null 2>&1; apt-get install -y iputils-ping >/dev/null 2>&1
  elif command -v dnf     >/dev/null; then dnf install -y iputils >/dev/null 2>&1
  elif command -v yum     >/dev/null; then yum install -y iputils >/dev/null 2>&1
  elif command -v apk     >/dev/null; then apk add iputils >/dev/null 2>&1
  else return 1; fi
  ping_supports_4
}

# 向导里调用: ping 存在但不是 iputils 时, 说清楚并尝试换掉
check_ping_variant(){
  local v; v=$(ping_variant)
  case "$v" in
    iputils) return 0 ;;
    none)    return 1 ;;   # 没装 ping, 由原有分支处理
  esac
  echo
  case "$v" in
    inetutils) warn "本机的 ping 是 GNU inetutils 版, 它不认 -4 参数." ;;
    busybox)   warn "本机的 ping 是 BusyBox 版, 参数不全." ;;
    *)         warn "本机的 ping 不认 -4 参数." ;;
  esac
  warn "  后果: 自动选对端会拿不到任何节点的延迟, 最后报「公共测速服务器暂时都不可用」."
  warn "  那句话是在甩锅给对端 —— 其实是本机 ping 的问题."
  echo
  echo "  换成标准的 iputils-ping 就好. 它只替换 ping 这一个包,"
  echo "  inetutils 的其他命令（telnet 等）不受影响, ping6 也仍然在."
  echo
  if confirm "  现在换？" y; then
    if install_iputils; then
      ok "已换成 $(ping -V 2>&1 | head -1)"
      return 0
    fi
    warn "换失败了."
    explain_pkg_failure iputils-ping
    warn "手动装: apt install -y iputils-ping  /  dnf install -y iputils"
    warn "或者在选对端那一步手动填一个 iperf3 服务器地址."
  else
    warn "跳过. 自动选对端多半会失败, 到时候手动填对端地址."
  fi
  return 1
}

# ── 运行计数 ────────────────────────────────────────────────────────────────
# 纯计数: 跑一次算一次, 不生成也不发送任何机器标识, 服务端无法区分
# "一台机器跑十次" 和 "十台各跑一次". 只额外带版本号, 用来判断旧版还有多少人在用.
#
# 关掉:  TCPFIT_NO_TELEMETRY=1   或   touch /var/lib/tcpfit/no-telemetry
#
# 三条硬约束:
#   1. 后台发, 绝不阻塞任何一步 —— 统计挂了/域名没了/用户在墙内, 调优照跑
#   2. 超时 3 秒, 失败静默
#   3. 界面显示的是【上一次拿到的】缓存值, 所以永远不会为它等待
STATS_URL="https://tcpfit.spacevps.cc/ping"
STATS_CACHE="$STATE_DIR/stats.json"

telemetry_off(){
  [ -n "${TCPFIT_NO_TELEMETRY:-}" ] && return 0
  [ -f "$STATE_DIR/no-telemetry" ] && return 0
  return 1
}

# 后台打一次, 结果写进缓存供【下次】显示. 不等待, 不检查返回码.
telemetry_ping(){
  telemetry_off && return 0
  command -v curl >/dev/null 2>&1 || return 0
  mkdir -p "$STATE_DIR" 2>/dev/null || return 0
  (
    out=$(curl -fsS --max-time 3 "${STATS_URL}?v=${VERSION}" 2>/dev/null) || exit 0
    # 只接受长得像 {"today":N,"total":N} 的东西, 别把错误页写进缓存
    case "$out" in
      *'"today"'*'"total"'*) printf '%s' "$out" > "$STATS_CACHE" 2>/dev/null ;;
    esac
  ) >/dev/null 2>&1 &
  return 0
}

# 读缓存, 给 banner 用. 没有缓存就返回空, banner 那一行整个不显示.
telemetry_line(){
  telemetry_off && return 0
  [ -f "$STATS_CACHE" ] || return 0
  local t n
  t=$(sed -n 's/.*"today"[[:space:]]*:[[:space:]]*\([0-9]*\).*/\1/p' "$STATS_CACHE" 2>/dev/null)
  n=$(sed -n 's/.*"total"[[:space:]]*:[[:space:]]*\([0-9]*\).*/\1/p' "$STATS_CACHE" 2>/dev/null)
  [ -n "$t" ] && [ -n "$n" ] && [ "$n" -gt 0 ] 2>/dev/null || return 0
  printf '今天 %s 次 · 累计 %s 次' "$t" "$n"
}

take_snapshot(){
  mkdir -p "$STATE_DIR"
  [ -f "$SNAPSHOT" ] && { info "Snapshot already exists, keeping the earliest one"; return; }
  # 机器已经被调过（手工或旧版本）却没有快照时, 当前状态不能当基线 ——
  # 那样 rollback 只会回到"调优后", 永远回不到出厂. 必须让用户先明确基线.
  if [ -f "$SYSCTL_FILE" ] || [ -f "$QDISC_SCRIPT" ]; then
    warn "检测到本机已有调优配置, 但没有出厂快照."
    warn "现在存快照会把「已调优状态」误记成基线, 导致 rollback 失效."
    warn "请先二选一："
    warn "  a) 手工写好出厂值到 $SNAPSHOT（格式见 docs）"
    warn "  b) 先 $(disp) rollback 回到出厂, 再重新 tune"
    warn "  c) 确认无需回滚能力, 则: touch $SNAPSHOT"
    die "已中止, 未做任何改动" 1
  fi
  local iface; iface=$(detect_iface)
  {
    echo "# tcpfit pre-tune snapshot  $(date -u +%FT%TZ)"
    echo "KERNEL=$(uname -r)"
    for k in $TUNED_KEYS; do
      printf '%s = %s\n' "$k" "$(sysctl -n "$k" 2>/dev/null)"
    done
    echo "# route: $(ip route show default)"
    echo "# qdisc: $(tc qdisc show dev "$iface" 2>/dev/null | head -1)"
  } > "$SNAPSHOT"
  ok "Snapshot saved: $SNAPSHOT"
  # 出厂状态同时存成 0000 存档 —— 名字固定, 后面不允许改名或删除
  mkdir -p "$ARCHIVE_DIR" 2>/dev/null
  [ -n "$(archive_path 0 2>/dev/null)" ] || {
    cp -a "$SNAPSHOT" "$ARCHIVE_DIR/0000-${ARCHIVE_ZERO_NAME}.snap" 2>/dev/null &&
      info "已建立存档 0000 ${ARCHIVE_ZERO_NAME}（出厂状态, 不可改名/删除）"
  }
}

# ── 调优存档 ────────────────────────────────────────────────────────────────
# 每次调优存一份完整状态: 参数 + sysctl + 路由 initcwnd + 整形值 + 拐点结果.
# 0000 是出厂状态, 名字固定、不可改名、不可删除、永远排最前 —— 它是最后的退路,
# 一旦被改写或删掉, 机器就再也回不到没被动过的样子.
ARCHIVE_DIR="$STATE_DIR/archives"
ARCHIVE_ZERO_NAME="出厂状态"

# 名字里不能有 / 和换行(会破坏文件名和逐行解析), 长度截到 40 个字符.
# 字节数转人话. 212992 显示成 0MB 会让人以为没设置, 所以小于 1MB 时用 KB.
# 2026-09-04T01:25:42Z -> "2026-09-04 01:25". 老快照是裸日期, 原样返回.
fmt_created(){
  local c="${1:-}" t
  case "$c" in
    *T*Z) t="${c#*T}"; printf '%s %s' "${c%%T*}" "$(printf '%s' "$t" | cut -c1-5)" ;;
    "")   printf '?' ;;
    *)    printf '%s' "$c" ;;
  esac
}

human_bytes(){
  local b="${1:-0}"
  if   [ "$b" -ge 1048576 ] 2>/dev/null; then printf '%d MB' $(( b / 1048576 ))
  elif [ "$b" -ge 1024 ]    2>/dev/null; then printf '%d KB' $(( b / 1024 ))
  else printf '%s B' "$b"; fi
}

archive_sanitize(){ printf '%s' "$1" | tr -d '\n\r/' | cut -c1-40; }

archive_path(){   # 按序号取存档路径
  local seq="$1"
  case "$seq" in ''|*[!0-9]*) return 1 ;; esac
  # 用字符串去掉前导零再补齐，避免八进制和超长数字的整数溢出。
  while [ "${#seq}" -gt 1 ] && [ "${seq#0}" != "$seq" ]; do seq=${seq#0}; done
  seq=$(printf '%4s' "$seq"); seq=${seq// /0}
  find "$ARCHIVE_DIR" -maxdepth 1 -name "${seq}-*.snap" 2>/dev/null | head -1
}
archive_seq_of(){ local b; b=$(basename "$1"); printf '%s' "${b%%-*}"; }
archive_name_of(){ local b; b=$(basename "$1" .snap); printf '%s' "${b#*-}"; }

archive_next_seq(){
  local last=-1 f s
  for f in "$ARCHIVE_DIR"/*.snap; do
    [ -e "$f" ] || continue
    s=$(archive_seq_of "$f"); s=$((10#$s))
    [ "$s" -gt "$last" ] && last=$s
  done
  printf '%04d' $(( last + 1 ))
}

# 老版本只有 pre-tune.snapshot. 有它没有 0000 时, 原样搬成 0000 ——
# 它就是那台机器的出厂状态, 不能丢.
archive_migrate(){
  mkdir -p "$ARCHIVE_DIR" 2>/dev/null || return 0
  [ -n "$(archive_path 0)" ] && return 0
  [ -f "$SNAPSHOT" ] || return 0
  cp -a "$SNAPSHOT" "$ARCHIVE_DIR/0000-${ARCHIVE_ZERO_NAME}.snap" 2>/dev/null &&
    info "已把旧快照收进存档 0000 ${ARCHIVE_ZERO_NAME}"
}

# archive_write <序号> <名字>  —— 把当前状态写成存档
archive_write(){
  local seq="$1" name="$2" iface rate f tmp
  mkdir -p "$ARCHIVE_DIR" || return 1
  iface=$(detect_iface)
  rate=$(tc_rate_mbit "$(tc class show dev "$iface" 2>/dev/null)")
  f="$ARCHIVE_DIR/${seq}-${name}.snap"
  tmp=$(mktemp "$ARCHIVE_DIR/.archive.XXXXXX") || return 1
  {
    echo "# tcpfit archive"
    echo "SEQ=$seq"
    echo "NAME=$name"
    echo "CREATED=$(date -u +%FT%TZ)"
    echo "CREATED_LOCAL=$(date +'%F %T %Z')"
    echo "TZ_NAME=$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo '?')"
    echo "TCPFIT_VERSION=$VERSION"
    echo "KERNEL=$(uname -r)"
    [ -n "${ARCH_ROLE:-}" ] && echo "PARAM_ROLE=$ARCH_ROLE"
    [ -n "${ARCH_BW:-}"   ] && echo "PARAM_BW=$ARCH_BW"
    [ -n "${ARCH_RTT:-}"  ] && echo "PARAM_RTT=$ARCH_RTT"
    [ -n "${ARCH_PEER:-}" ] && echo "PARAM_PEER=$ARCH_PEER"
    [ -n "${ARCH_MODE:-}" ] && echo "PARAM_MODE=$ARCH_MODE"
    [ -n "${ARCH_RUN:-}" ] && echo "PARAM_RUN=$ARCH_RUN"
    [ -n "${ARCH_RETURN_SNAPSHOT:-}" ] && echo "RETURN_SNAPSHOT=$ARCH_RETURN_SNAPSHOT"
    echo "SHAPE_RATE=${rate:-none}"
    [ "${ARCH_INCLUDE_SWEEP:-1}" = 1 ] && [ -f "$STATE_DIR/sweep.result" ] &&
      sed 's/^/SWEEP_/' "$STATE_DIR/sweep.result"
    for k in $TUNED_KEYS; do
      printf '%s = %s\n' "$k" "$(sysctl -n "$k" 2>/dev/null)"
    done
    echo "# route: $(ip route show default)"
    echo "# qdisc: $(tc qdisc show dev "$iface" 2>/dev/null | head -1)"
  } > "$tmp" || { rm -f "$tmp"; return 1; }
  mv -- "$tmp" "$f" || { rm -f "$tmp"; return 1; }
  printf '%s' "$f"
}

# archive_save [名字]  —— 存一份新的. 名字空则用日期时间.
archive_save(){
  archive_migrate
  local name seq f
  name=$(archive_sanitize "${1:-}")
  [ -n "$name" ] || name="$(date +%m%d-%H%M)"
  seq=$(archive_next_seq)
  # 0000 只能由 take_snapshot 在出厂状态下创建, 不接受用户占位
  [ "$seq" = 0000 ] && { warn "还没有出厂快照, 先跑一次 tune 或 rollback"; return 1; }
  f=$(archive_write "$seq" "$name") || { warn "存档写入失败: $name"; return 1; }
  ok "已存档 $seq $name"
  info "  $f"
}

archive_list(){
  archive_migrate
  local f seq name created ver rate n=0 cur_cc cur_rate mark marked=0
  [ -d "$ARCHIVE_DIR" ] || { warn "还没有任何存档"; return 0; }
  # 判断"当前处于哪个存档": 拿现在的拥塞控制+整形值去比对
  cur_cc=$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null)
  cur_rate=$(tc_rate_mbit "$(tc class show dev "$(detect_iface)" 2>/dev/null)")
  local cur_rmem; cur_rmem=$(sysctl -n net.core.rmem_max 2>/dev/null)
  echo
  printf '  %s %s %s %s %s\n' "$(_pad 序号 6)" "$(_pad 名字 20)" "$(_pad '时间 (UTC)' 18)" "$(_pad 整形 8)" "版本"
  printf '  %s\n' "$(printf '─%.0s' $(seq 1 68))"
  while IFS= read -r f; do
    seq=$(archive_seq_of "$f"); name=$(archive_name_of "$f")
    created=$(awk -F= '/^CREATED=/{print $2; exit}' "$f")
    [ -n "$created" ] || created=$(awk '/^# tcpfit pre-tune snapshot/{print $NF; exit}' "$f")
    ver=$(awk -F= '/^TCPFIT_VERSION=/{print $2; exit}' "$f")
    rate=$(awk -F= '/^SHAPE_RATE=/{print $2; exit}' "$f")
    case "$rate" in none|"") rate="-" ;; *) rate="${rate}M" ;; esac
    # 当前状态标记: 整形值对得上就算(拥塞控制太粗, 多个存档可能都是 bbr)
    # 判"当前"要三个都对上: 整形值 + 拥塞控制 + 缓冲区上限.
    # 只看整形值不够 —— 出厂和调优后都可能是"无整形", 会标错到出厂那行.
    mark=""
    if [ "$marked" = 0 ]; then
      local a_cc a_rmem
      a_cc=$(awk -F'[ =]+' '/^net\.ipv4\.tcp_congestion_control/{print $2; exit}' "$f")
      a_rmem=$(awk -F'[ =]+' '/^net\.core\.rmem_max/{print $2; exit}' "$f")
      if { { [ "$rate" = "-" ] && [ -z "$cur_rate" ]; } || [ "$rate" = "${cur_rate}M" ]; } &&
         [ "$a_cc" = "$cur_cc" ] && [ "$a_rmem" = "$cur_rmem" ]; then
        mark=" ${green}<= 当前${plain}"; marked=1
      fi
    fi
    n=$((n+1))
    printf '  %s %s %s %s %s%s%s\n' \
      "$(_pad "$seq" 6)" "$(_pad "$name" 20)" "$(_pad "$(fmt_created "$created")" 18)" \
      "$(_pad "$rate" 8)" "${ver:--}" \
      "$([ "$seq" = 0000 ] && printf ' %s' "$(_c '0;33' '(出厂, 不可改名/删除)')")" "$mark"
  done < <(find "$ARCHIVE_DIR" -maxdepth 1 -name '*.snap' 2>/dev/null | sort)
  [ "$n" = 0 ] && warn "还没有任何存档"
  echo
  return 0
}

# 找存档: 接受序号(0/0000/1) 或名字
# 查找顺序: 纯数字当序号, 否则当名字.
# 所以名字取成纯数字(比如 "123")时会被当序号找, 找不到才回退 —— 别那样命名.
# 按名字找时要处理重名: 自动名是 base-<带宽>-rtt<RTT>, 同一分钟跑两次 tune
# 就会撞名. 早期实现返回第一个匹配(序号最小的那个), 于是"恢复我那个
# base-220M-rtt150"会静默恢复成较旧的一份 —— 配置不一样, 用户看不出来.
# 现在重名就列出候选并要求用序号, 宁可多问一句.
archive_find_by_name(){
  local want="$1" f hits=0 last=""
  for f in "$ARCHIVE_DIR"/*.snap; do
    [ -e "$f" ] || continue
    [ "$(archive_name_of "$f")" = "$want" ] || continue
    hits=$(( hits + 1 )); last="$f"
  done
  [ "$hits" = 0 ] && return 1
  if [ "$hits" -gt 1 ]; then
    warn "有 ${hits} 个存档都叫 ${want}, 请改用序号:" >&2
    for f in "$ARCHIVE_DIR"/*.snap; do
      [ -e "$f" ] || continue
      [ "$(archive_name_of "$f")" = "$want" ] || continue
      echo "    $(archive_seq_of "$f")   $want" >&2
    done
    return 2          # 2 = 重名, 提示已经打过, 调用方不要再说"找不到"
  fi
  printf '%s' "$last"; return 0
}

archive_find(){
  local want="$1" f
  case "$want" in
    ''|*[!0-9]*) archive_find_by_name "$want" ;;
    *)           f=$(archive_path "$want"); [ -n "$f" ] && { printf '%s' "$f"; return 0; }
                 # 序号没命中, 再按名字找一遍(有人可能真把存档叫 "123")
                 archive_find_by_name "$want" ;;
  esac
}

# 恢复即时路由，并让 hook 只重放存档中的窗口值；网关/地址沿用启动时的路由。
archive_restore_route(){
  local route="$1" token skip=0 tmp
  local -a args=() windows=()
  rm -f "$ROUTE_HOOK" "$INITCWND_MARKER" || return 1
  [ -n "$route" ] || return 0
  read -r -a args <<< "$route"
  for token in "${args[@]}"; do
    if [ "$skip" = 1 ]; then
      case "$token" in ''|*[!0-9]*) warn "存档中的路由窗口无效"; return 1 ;; esac
      windows+=("$token"); skip=0
    else
      case "$token" in initcwnd|initrwnd) windows+=("$token"); skip=1 ;; esac
    fi
  done
  [ "$skip" = 0 ] || return 1
  ip -4 route replace "${args[@]}" 2>/dev/null || { warn "默认路由还原失败"; return 1; }
  if [ "${#windows[@]}" -gt 0 ]; then
    if [ ! -d "$(dirname "$ROUTE_HOOK")" ]; then
      warn "路由已即时还原，但缺少 networkd-dispatcher hook 目录，无法持久化窗口值"
      return 1
    fi
    tmp=$(mktemp "${ROUTE_HOOK}.restore.XXXXXX") || return 1
    {
      printf '#!/bin/bash\nwindows=('
      printf ' %q' "${windows[@]}"
      printf ' )\n'
      cat <<'H'
routes=$(ip -4 route show default)
route=${routes%%$'\n'*}
[ -n "$route" ] || exit 0
read -r -a args <<< "$route"
clean=(); skip=0
for token in "${args[@]}"; do
  if [ "$skip" = 1 ]; then skip=0; continue; fi
  case "$token" in initcwnd|initrwnd) skip=1 ;; *) clean+=("$token") ;; esac
done
ip -4 route replace "${clean[@]}" "${windows[@]}"
H
    } > "$tmp" || { rm -f "$tmp"; return 1; }
    chmod 755 "$tmp" && mv -- "$tmp" "$ROUTE_HOOK" || { rm -f "$tmp"; return 1; }
    mkdir -p "$STATE_DIR" && : > "$INITCWND_MARKER" || return 1
  fi
  ok "默认路由已还原，窗口持久化已同步"
}

archive_restore(){
  archive_migrate
  local f="$1" iface rate route was_cc was_rmem was_rate now_cc now_rmem now_rate
  local tmp k v failed=0
  [ -r "$f" ] && [ -f "$f" ] || { warn "无法读取存档: $f"; return 1; }
  local return_snapshot
  return_snapshot=$(sed -n 's/^RETURN_SNAPSHOT=//p' "$f")
  if [ -n "$return_snapshot" ]; then
    [ -f "$return_snapshot" ] || { warn "优化线路调优完整配置文件缺失: $return_snapshot，未执行部分恢复"; return 1; }
    return_assets || { warn "无法加载优化线路调优恢复模块"; return 1; }
    python3 "$RETURN_HELPER" restore --snapshot "$return_snapshot" --lock-fd 9 || return 1
    ok "已恢复存档 $(archive_seq_of "$f") 的完整参数、队列和持久化配置"
    return 0
  fi
  # 出厂回滚必须移除持久化入口，与 rollback 使用同一路径。
  if [ "$(archive_seq_of "$f")" = 0000 ]; then
    local SNAPSHOT="$f"
    cmd_rollback
    return $?
  fi
  grep -qE '^(net|vm|fs)\.[^=]+=' "$f" || { warn "存档没有 sysctl 数据"; return 1; }
  rate=$(awk -F= '/^SHAPE_RATE=/{print $2; exit}' "$f")
  if [ -n "$rate" ] && [ "$rate" != none ]; then
    is_posint "$rate" 1 100000 || { warn "存档中的整形值无效: $rate"; return 1; }
  fi
  iface=$(detect_iface)
  [ -n "$iface" ] || { warn "找不到默认路由网卡"; return 1; }
  tmp=$(mktemp "${SYSCTL_FILE}.restore.XXXXXX") || return 1
  was_cc=$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null)
  was_rmem=$(sysctl -n net.core.rmem_max 2>/dev/null)
  was_rate=$(tc_rate_mbit "$(tc class show dev "$iface" 2>/dev/null)")
  info "回滚到存档 $(archive_seq_of "$f") $(archive_name_of "$f")…"
  # 同步启动配置；本内核拒绝的值不留成启动时必然失败的配置项。
  printf '# tcpfit restored archive %s\n' "$(archive_seq_of "$f")" > "$tmp"
  if grep -qE '^net\.ipv4\.tcp_congestion_control[[:space:]]*=[[:space:]]*bbr[[:space:]]*$' "$f"; then
    modprobe tcp_bbr 2>/dev/null || true
  fi
  while IFS='=' read -r k v; do
    k=$(echo "$k" | xargs); v=$(echo "$v" | xargs)
    [ -n "$k" ] && [ -n "$v" ] || continue
    if sysctl -qw "$k=$v" 2>/dev/null; then
      printf '%s = %s\n' "$k" "$v" >> "$tmp" || failed=1
    else
      warn "参数还原失败: $k"
      printf '# 本内核拒绝: %s = %s\n' "$k" "$v" >> "$tmp"
      failed=1
    fi
  done < <(grep -E '^(net|vm|fs)\.' "$f")
  chmod 644 "$tmp" && mv -- "$tmp" "$SYSCTL_FILE" || {
    rm -f "$tmp"; warn "sysctl 启动配置写入失败，运行值可能已改变"; return 1;
  }
  [ "$failed" = 0 ] && ok "sysctl 已按存档还原并持久化"
  if [ "$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null)" = bbr ]; then
    echo tcp_bbr > "$BBR_MODULE_FILE" || failed=1
  else
    rm -f "$BBR_MODULE_FILE" || failed=1
  fi
  # 路由(initcwnd)
  route=$(awk '/^# route: /{sub(/^# route: /, ""); print; exit}' "$f")
  archive_restore_route "$route" || failed=1
  # 整形
  if [ -n "$rate" ] && [ "$rate" != none ]; then
    if cmd_shape --rate "$rate" >/dev/null 2>&1 &&
       systemctl is-enabled tcpfit-qdisc.service >/dev/null 2>&1; then
      ok "整形已还原为 ${rate} Mbit，开机服务已启用"
    else warn "整形还原失败"; failed=1; fi
  else
    local shape_failed=0 default_kind
    if ! systemctl disable --now tcpfit-qdisc.service >/dev/null 2>&1; then
      # 未安装服务的干净机器无需停用；已有服务失败则必须报告。
      if [ -f "$QDISC_UNIT" ] || [ -f "$QDISC_SCRIPT" ]; then shape_failed=1; fi
    fi
    rm -f "$QDISC_UNIT" "$QDISC_SCRIPT" || shape_failed=1
    systemctl daemon-reload >/dev/null 2>&1 || shape_failed=1
    # sysctl 已还原。删除整形后让内核补回其默认队列，不强制改成 fq。
    if ! qdisc_remove_root "$iface"; then
      default_kind=$(sysctl -n net.core.default_qdisc 2>/dev/null)
      [ -n "$default_kind" ] && [ "$(qdisc_root_kind "$iface")" = "$default_kind" ] || shape_failed=1
    fi
    if [ "$shape_failed" = 0 ]; then ok "整形已移除，开机整形服务已停用"
    else warn "整形移除或服务停用失败"; failed=1; fi
  fi
  now_cc=$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null)
  now_rmem=$(sysctl -n net.core.rmem_max 2>/dev/null)
  echo
  printf '  %s %s   %s\n' "$(_pad '' 12)" "$(_pad 回滚前 14)" "回滚后"
  printf '  %s %s   %s\n' "$(_pad 拥塞控制 12)" "$(_pad "$was_cc" 14)" "$now_cc"
  printf '  %s %s   %s\n' "$(_pad 缓冲区上限 12)" "$(_pad "$(human_bytes "$was_rmem")" 14)" "$(human_bytes "$now_rmem")"
  now_rate=$(tc_rate_mbit "$(tc class show dev "$iface" 2>/dev/null)")
  printf '  %s %s   %s\n' "$(_pad 整形 12)" "$(_pad "${was_rate:-无}${was_rate:+M}" 14)" "${now_rate:-无}${now_rate:+M}"
  echo
  [ "$failed" = 0 ] || warn "存档仅部分还原，请检查以上失败项"
  return "$failed"
}

archive_rename(){
  local f="$1" new; new=$(archive_sanitize "$2")
  [ "$(archive_seq_of "$f")" = 0000 ] && { warn "0000 ${ARCHIVE_ZERO_NAME} 是出厂状态, 不能改名"; return 1; }
  [ -n "$new" ] || { warn "新名字不能为空"; return 1; }
  mv "$f" "$(dirname "$f")/$(archive_seq_of "$f")-${new}.snap" && ok "已改名为 $new"
}

archive_delete(){
  local f="$1"
  [ "$(archive_seq_of "$f")" = 0000 ] && { warn "0000 ${ARCHIVE_ZERO_NAME} 是最后的退路, 不能删除"; return 1; }
  rm -f "$f" && ok "已删除存档 $(archive_seq_of "$f")"
}

cmd_archive(){
  need_root
  take_lock
  migrate_legacy
  archive_migrate
  local sub="${1:-list}"; shift 2>/dev/null || true
  case "$sub" in
    list|ls|"")  archive_list ;;
    save)        take_lock; archive_save "${1:-}" ;;
    restore)     [ -n "${1:-}" ] || die "用法: $(disp) archive restore <序号|名字>"
                 take_lock
                 local f rc=0; f=$(archive_find "$1") || rc=$?
                 [ "$rc" = 2 ] && return 1                       # 重名, 上面已列出候选
                 [ -n "$f" ] || die "找不到存档: $1"
                 archive_restore "$f" ;;
    rename)      [ -n "${2:-}" ] || die "用法: $(disp) archive rename <序号> <新名字>"
                 local f rc=0; f=$(archive_find "$1") || rc=$?
                 [ "$rc" = 2 ] && return 1                       # 重名, 上面已列出候选
                 [ -n "$f" ] || die "找不到存档: $1"
                 archive_rename "$f" "$2" ;;
    delete|rm)   [ -n "${1:-}" ] || die "用法: $(disp) archive delete <序号>"
                 local f rc=0; f=$(archive_find "$1") || rc=$?
                 [ "$rc" = 2 ] && return 1                       # 重名, 上面已列出候选
                 [ -n "$f" ] || die "找不到存档: $1"
                 archive_delete "$f" ;;
    show|cat)    [ -n "${1:-}" ] || die "用法: $(disp) archive show <序号>"
                 local f rc=0; f=$(archive_find "$1") || rc=$?
                 [ "$rc" = 2 ] && return 1                       # 重名, 上面已列出候选
                 [ -n "$f" ] || die "找不到存档: $1"
                 cat "$f" ;;
    *)           die "未知子命令: $sub（list / save / restore / rename / delete / show）" ;;
  esac
}

cmd_uninstall(){
  need_root
  take_lock
  migrate_legacy
  local keep_archives=0 yes=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --keep-archives) keep_archives=1; shift ;;
      --yes|-y) yes=1; shift ;;
      *) die "未知参数: $1（--keep-archives / --yes）" ;;
    esac
  done

  echo
  warn "卸载会做这些事："
  echo "    1. 回滚到出厂状态（sysctl / initcwnd / 整形全部还原）"
  echo "    2. 删掉 $SYSCTL_FILE"
  echo "    3. 停用并删掉 tcpfit-qdisc 服务和脚本"
  echo "    4. 删掉 $ROUTE_HOOK（开机写回 initcwnd 的 hook）"
  if [ "$keep_archives" = 1 ]; then
    echo "    5. 保留 $STATE_DIR（存档和快照）"
  else
    echo "    5. 删掉 $STATE_DIR（存档和快照, 之后无法再回滚）"
  fi
  echo "    6. 删掉 $SELF_PATH 和 /usr/local/lib/tcpfit 中的程序模块"
  echo
  echo "  不会碰: swap（要删自己 swapoff）、iperf3、ping 等装过的包."
  echo
  [ "$yes" = 1 ] || confirm "  确定卸载？" n || { info "已取消, 什么都没动"; return 0; }

  # 这里不要再 info "回滚中…" —— cmd_rollback 自己会打, 否则屏幕上出现两遍.
  cmd_rollback || { warn "回滚未完全成功，已停止卸载并保留存档，请处理失败项后重试"; return 1; }

  # 配置和服务已由 cmd_rollback 清理并检查，不再重复执行。
  ok "配置和服务已移除"

  # 保留当前脚本到最后；旧入口清理失败时也保留存档，便于重试。
  rm -f "$LEGACY_SELF" 2>/dev/null || { warn "旧入口删除失败: $LEGACY_SELF，已停止卸载"; return 1; }
  rm -rf /usr/local/lib/tcpfit || { warn "程序模块删除失败，已保留当前脚本供重试"; return 1; }
  if [ "$keep_archives" = 1 ]; then
    info "存档保留在 $STATE_DIR"
  else
    rm -rf "$STATE_DIR" || { warn "存档目录未完全删除: $STATE_DIR，已保留当前脚本供重试"; return 1; }
    ok "存档和快照已删除"
  fi

  # 最后删自己. 正在执行的脚本被删掉不影响当前进程(inode 还在), 但要放在最后.
  local me="$SELF_PATH"
  if [ -e "$me" ] || [ -L "$me" ]; then
    rm -f "$me" || { warn "脚本删除失败: $me，卸载未完成"; return 1; }
    ok "已删除 $me"
  fi
  echo
  ok "tcpfit 已卸载. 机器回到了出厂状态."
  echo "  重新装:  bash <(curl -fsSL $SELF_URL)"
}

cmd_rollback(){
  need_root
  take_lock
  migrate_legacy
  local purge_swap=0 failed=0 k v route
  while [ $# -gt 0 ]; do
    case "$1" in
      --purge-swap) purge_swap=1; shift ;;
      *) die "未知参数: $1" ;;
    esac
  done
  info "回滚中…"
  # 先取回滚前的值 —— 必须在删文件、改 sysctl 之前, 否则读到的就是回滚后的.
  # 恢复其他存档时有这张对照表, 唯独最常用的"回到出厂"没有, 不合理.
  local was_cc was_rmem was_rate now_cc now_rmem now_rate
  was_cc=$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null)
  was_rmem=$(sysctl -n net.core.rmem_max 2>/dev/null)
  was_rate=$(tc_rate_mbit "$(tc class show dev "$(detect_iface)" 2>/dev/null)")
  rm -f "$SYSCTL_FILE" "$ROUTE_HOOK" "$INITCWND_MARKER" "$BBR_MODULE_FILE" || {
    warn "调优配置未完全删除，请检查文件权限或只读文件系统"; failed=1;
  }
  local service_stopped=1
  if ! systemctl disable --now tcpfit-qdisc.service >/dev/null 2>&1; then
    # 没装过整形服务时 disable 也会失败；已有服务停用失败则保留文件供重试。
    if [ -e "$QDISC_UNIT" ] || [ -L "$QDISC_UNIT" ] || [ -e "$QDISC_SCRIPT" ] || [ -L "$QDISC_SCRIPT" ]; then
      warn "整形服务停用失败，已保留服务文件供重试"; failed=1; service_stopped=0
    fi
  fi
  if [ "$service_stopped" = 1 ]; then
    rm -f "$QDISC_UNIT" "$QDISC_SCRIPT" || { warn "整形服务文件未完全删除"; failed=1; }
  fi
  systemctl daemon-reload >/dev/null 2>&1 || { warn "systemd 配置重载失败"; failed=1; }
  local iface; iface=$(detect_iface)
  # 逐项写回快照值
  if [ -f "$SNAPSHOT" ]; then
    while IFS='=' read -r k v; do
      k=$(echo "$k" | xargs); v=$(echo "$v" | xargs)
      [ -n "$k" ] && [ -n "$v" ] || continue
      if ! sysctl -qw "$k=$v" 2>/dev/null; then warn "参数还原失败: $k"; failed=1; fi
    done < <(grep -E '^(net|vm|fs)\.' "$SNAPSHOT")
    [ "$failed" = 0 ] && ok "已按快照还原 sysctl"
    route=$(awk '/^# route: /{sub(/^# route: /, ""); print; exit}' "$SNAPSHOT")
    if [ -n "$route" ]; then
      local -a route_args=()
      read -r -a route_args <<< "$route"
      ip -4 route replace "${route_args[@]}" 2>/dev/null || { warn "默认路由还原失败"; failed=1; }
    fi
  else
    warn "找不到快照, 仅移除了调优文件；重启后内核默认值生效"
    failed=1
  fi
  # 先还原 default_qdisc，再移除整形；内核补回的默认队列才使用出厂值。
  if ! qdisc_remove_root "$iface"; then
    if [ "$(qdisc_root_kind "$iface")" = htb ]; then
      warn "整形移除失败"; failed=1
    fi
  fi
  # swap 默认不动 —— 删掉一个正在用的 swap 可能让机器立刻 OOM.
  # 想连 swap 一起撤销要显式加 --purge-swap.
  if [ "$purge_swap" = 1 ]; then
    if [ ! -f /swapfile ]; then
      info "没有 /swapfile, 跳过"
    elif [ ! -f "$STATE_DIR/swapfile.owned" ]; then
      warn "/swapfile 不是 tcpfit 创建的, 拒绝删除. 要删请自己确认后手动操作"
    elif ! swapoff /swapfile 2>/dev/null; then
      # swapoff 失败通常是内存不够把页换回来, 这时删文件会让内核继续写一个
      # 已删除的 inode, 空间也不会释放 —— 必须停手
      warn "swapoff /swapfile 失败（内存可能不足以换回), 未删除. 释放内存后重试"
    else
      rm -f /swapfile
      sed -i '\#^/swapfile #d' /etc/fstab
      rm -f "$STATE_DIR/swapfile.owned"
      ok "已移除 /swapfile 及其 fstab 条目"
    fi
  elif [ -f /swapfile ]; then
    info "/swapfile 保留. 要一并删除: $(disp) rollback --purge-swap"
  fi
  now_cc=$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null)
  now_rmem=$(sysctl -n net.core.rmem_max 2>/dev/null)
  now_rate=$(tc_rate_mbit "$(tc class show dev "$iface" 2>/dev/null)")
  echo
  printf '  %s %s   %s\n' "$(_pad '' 12)" "$(_pad 回滚前 14)" "回滚后"
  printf '  %s %s   %s\n' "$(_pad 拥塞控制 12)" "$(_pad "$was_cc" 14)" "$now_cc"
  printf '  %s %s   %s\n' "$(_pad 缓冲区上限 12)" "$(_pad "$(human_bytes "$was_rmem")" 14)" "$(human_bytes "$now_rmem")"
  printf '  %s %s   %s\n' "$(_pad 整形 12)" "$(_pad "${was_rate:-无}${was_rate:+M}" 14)" "${now_rate:-无}${now_rate:+M}"
  echo
  if [ "$failed" = 0 ]; then ok "回滚完成"
  else warn "仅部分回滚成功，请检查以上失败项"; fi
  return "$failed"
}

# ── 基础调优 ────────────────────────────────────────────────────────────────
cmd_tune(){
  need_root
  take_lock
  migrate_legacy
  self_install
  local role=mixed bw="" rtt="" no_initcwnd=0 peer="" ARCH_SAVE_NAME=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --role) role="$2"; shift 2 ;;
      --bw)   bw="$2";   shift 2 ;;
      --rtt)  rtt="$2";  shift 2 ;;
      --peer) peer="$2"; shift 2 ;;
      --no-initcwnd) no_initcwnd=1; shift ;;
      --save) ARCH_SAVE_NAME="$2"; shift 2 ;;
      *) die "未知参数: $1" ;;
    esac
  done
  case "$role" in proxy|bulk|mixed) ;; *) die "role 只能是 proxy / bulk / mixed" ;; esac

  local iface ram; iface=$(detect_iface); ram=$(detect_ram_mb)
  [ -n "$iface" ] || die "找不到默认路由网卡"
  # --rtt 给了就用给的, 没给就用固定值. 不再探测, 所以不会再出现
  # "无法确定 RTT" 这种把用户指向死路的报错（向导里根本没地方填 --rtt）.
  if [ -n "$rtt" ]; then
    is_posint "$rtt" 1 2000 || die "--rtt 必须是 1-2000 之间的整数（毫秒）"
  else
    rtt="$DEFAULT_RTT"
  fi
  # --bw auto: 现场探测. 虚拟网卡读不到标称速率, 这是最常见的情况.
  if [ "$bw" = auto ]; then
    [ -n "$peer" ] || die "--bw auto 需要同时给 --peer <近处的iperf3服务器>"
    command -v iperf3 >/dev/null || die "--bw auto 需要 iperf3"
    info "Probing available bandwidth..."
    bw=$(probe_bandwidth "$peer" "$iface") || bw=""
    [ -n "$bw" ] && ok "Measured ~${bw} Mbps" || die "bandwidth probe failed" 2
  fi
  [ -n "$bw" ] || bw=$(detect_link_mbps "$iface")
  if ! { [ -n "$bw" ] && [ "$bw" -gt 0 ] 2>/dev/null; }; then
    warn "本机是虚拟网卡, 读不到标称速率. 三选一："
    warn "  a) 知道套餐带宽:  $(disp) tune --role $role --bw <Mbps>"
    warn "  b) 现场探测:      $(disp) tune --role $role --bw auto --peer <近处iperf3服务器>"
    warn "  c) 先单独探测:    $(disp) probe --peer <近处iperf3服务器>"
    die "无法确定带宽, 已中止" 1
  fi

  take_snapshot

  local bdp buf_max buf_def buf_min tcp_mem
  local buf_limit buffer_profile
  buffer_profile=$(calc_buffer_profile "$role" "$bw" "$rtt" "$ram") || die "缓冲区推导失败，未应用调优参数"
  read -r bdp buf_max buf_def buf_limit <<<"$buffer_profile"
  buf_min=4096
  [ "$buf_min" -le "$buf_def" ] || buf_min="$buf_def"
  tcp_mem=$(calc_tcp_mem "$ram")

  info "Derived from: ${bw} Mbps / RTT ${rtt} ms / ${ram} MB RAM / role $role"
  kv "  BDP"            "$(format_mib "$bdp")"
  kv "  Buffer max"     "$(format_mib "$buf_max")  ($(buf_max_reason "$bdp" "$ram" "$buf_max"))"
  kv "  tcp_mem"        "$(echo "$tcp_mem" | awk '{printf "%.0fM / %.0fM / %.0fM", $1*4/1024, $2*4/1024, $3*4/1024}')  (RAM 1/16, 1/8, 1/4)"

  modprobe tcp_bbr 2>/dev/null
  echo tcp_bbr > "$BBR_MODULE_FILE"
  local cc=bbr
  has_word "$(sysctl -n net.ipv4.tcp_available_congestion_control 2>/dev/null)" bbr || {
    warn "kernel has no BBR, falling back to cubic (much smaller gain)"; cc=cubic; }

  cat > "$SYSCTL_FILE" <<EOF
# 由 tcpfit v$VERSION 生成  $(date -u +%FT%TZ)
# 带宽=${bw}Mbps  RTT=${rtt}ms  内存=${ram}MB  角色=${role}
# 完整调优用 tcpfit tune；只改缓冲区用 tcpfit buffer

net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = $cc

# 缓冲区：初始上限=${TCPFIT_BUFFER_FORMULA:-1.5×BDP+2MiB余量}, 默认值按角色（默认值决定爬坡快慢, 也决定每连接内存占用）
net.core.rmem_max = $buf_max
net.core.wmem_max = $buf_max
net.core.rmem_default = $buf_def
net.core.wmem_default = $buf_def
net.ipv4.tcp_rmem = $buf_min $buf_def $buf_max
net.ipv4.tcp_wmem = $buf_min $buf_def $buf_max
# 全局 TCP 内存上限, 按物理内存推导. 设太高是小内存机 OOM 的主因.
net.ipv4.tcp_mem = $tcp_mem

net.ipv4.tcp_window_scaling = 1
net.ipv4.tcp_moderate_rcvbuf = 1
net.ipv4.tcp_adv_win_scale = 1

net.core.netdev_max_backlog = 16384
# netdev_budget / budget_usecs 都测过了, 【没有可测量的差别】, 所以维持原值.
# 唯一可信的那组是自有对端(4 台并发 3.75G, n=5, 变异系数 0%):
#   budget 600(现值) 3751 Mbps   budget 300(内核默认) 3745 Mbps   差 0.16%
#   两边 softnet_drop 都是 0, time_squeeze 相当.
#   数据: results/highbw-20260811/netdev-budget300-vs600-multisource.tsv
# ⚠ 别拿公共 10G 对端(speedtest.lax12)测这个 —— 那几组变异系数 23%~45%,
#   同一个变体既能跑 9395 也能跌到 2817, 噪声完全压过参数效应.
#   曾经据此误判成"600 有害", 复核后推翻. 要重测请用自有对端.
net.core.netdev_budget = 600
net.core.netdev_budget_usecs = 4000
net.core.optmem_max = 65536
net.core.somaxconn = 8192
net.ipv4.tcp_max_syn_backlog = 8192

net.ipv4.tcp_slow_start_after_idle = 0
net.ipv4.tcp_no_metrics_save = 0
net.ipv4.tcp_mtu_probing = 1
net.ipv4.tcp_sack = 1
net.ipv4.tcp_dsack = 1
net.ipv4.tcp_timestamps = 1
net.ipv4.tcp_fastopen = 3

net.ipv4.tcp_syncookies = 1
net.ipv4.tcp_tw_reuse = 1
net.ipv4.tcp_fin_timeout = 15
net.ipv4.tcp_keepalive_time = 600
net.ipv4.ip_local_port_range = 1024 65535

vm.min_free_kbytes = 32768
fs.file-max = 1000000

# 刻意不设的项:
#   tcp_notsent_lowat  —— 低核数机器上压吞吐
#   tcp_reordering=300 —— 现代内核走 RACK, 调高只推迟快速重传
EOF

  # 逐项校验并把内核拒绝的项注释掉.
  #
  # 为什么不解析 `sysctl -p` 的报错文本: 格式随 procps 版本和 locale 变
  # （"cannot stat /proc/sys/..." / "setting key \"net.core.x\"" / 本地化过的消息），
  # 实测非 ASCII 键名还会把正则截断. 直接试写最可靠.
  #
  # 为什么必须注释掉而不只是警告: 被拒的项留在文件里, systemd-sysctl.service
  # 每次开机都会 failed（issue #9: Debian 13 / 6.12 拒绝 netdev_budget_usecs=4000）.
  # 用户看到的是一个红色的系统服务, 而不是"某个参数没生效".
  local _bad=0 _line _k _v _path
  while IFS= read -r _line; do
    case "$_line" in \#*|'') continue ;; esac
    case "$_line" in *=*) ;; *) continue ;; esac
    _k=$(printf '%s' "${_line%%=*}" | tr -d ' \t')
    _v=$(printf '%s' "${_line#*=}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
    [ -n "$_k" ] || continue
    _path="/proc/sys/$(printf '%s' "$_k" | tr '.' '/')"
    if [ ! -e "$_path" ] || ! sysctl -qw "$_k=$_v" 2>/dev/null; then
      # 用固定分隔符 | 之外的字符做 sed 分隔, 键名里不会有 %
      sed -i "s%^[[:space:]]*${_k}[[:space:]]*=%# 本内核不支持, tcpfit 自动注释: ${_k} =%" "$SYSCTL_FILE"
      warn "  内核不接受  ${_k} = ${_v}  —— 已在配置里注释掉"
      _bad=$(( _bad + 1 ))
    fi
  done < "$SYSCTL_FILE"

  if [ "$_bad" -gt 0 ]; then
    ok "sysctl applied: $SYSCTL_FILE（$_bad 项被本内核拒绝, 已注释, 不影响其他调优）"
  else
    ok "sysctl applied: $SYSCTL_FILE"
  fi

  if [ "$no_initcwnd" = 0 ]; then
    local gw; gw=$(detect_gw)
    if [ -n "$gw" ]; then
      if ip route replace default via "$gw" dev "$iface" initcwnd 32 initrwnd 32 2>/dev/null; then
        mkdir -p "$STATE_DIR"; : > "$INITCWND_MARKER"
        ok "initcwnd/initrwnd = 32"
      else
        warn "initcwnd not applied (unsupported on some hypervisors)"
      fi
      if [ -d /etc/networkd-dispatcher/routable.d ]; then
        cat > "$ROUTE_HOOK" <<'H'
#!/bin/bash
GW=$(ip route show default | awk '{print $3; exit}')
IF=$(ip route show default | awk '{print $5; exit}')
[ -n "$GW" ] && [ -n "$IF" ] && ip route replace default via "$GW" dev "$IF" initcwnd 32 initrwnd 32
exit 0
H
        chmod +x "$ROUTE_HOOK"
      fi
    fi
  elif clear_owned_initcwnd; then
    [ "${INITCWND_CLEARED:-0}" = 1 ] && ok "Low-bandwidth path: tcpfit initcwnd override removed"
  else
    warn "无法清除旧 initcwnd；已移除持久化 hook，当前路由请手工检查"
  fi

  # 存一份调优后的存档. 参数一起记进去, 以后翻存档能看出当时是按什么调的.
  ARCH_ROLE="$role"; ARCH_BW="$bw"; ARCH_RTT="$rtt"; ARCH_PEER="$peer"
  # 向导还要扫描并决定最终整形, 存档留到全部完成之后, 避免记录中间状态.
  [ "$WIZARD" = 1 ] && return 0
  archive_save "${ARCH_SAVE_NAME:-base-${bw}M-rtt${rtt}}" >/dev/null 2>&1 ||
    warn "调优已生效, 但存档没建成 —— rollback 仍可用($(disp) rollback), 存档功能可稍后手动 $(disp) archive save"

  info "基础调优完成. 下一步跑 sweep 找限速器拐点 —— 那才是大头."
  echo "  $(disp) sweep --peer <近处的iperf3服务器> --nominal $bw"

  # 小内存机不加 swap 就是定时炸弹：实测过 tcp_mem 撑爆内存把代理进程连杀 7 次
  if [ "$ram" -le 1024 ] && ! not_blank "$(swapon --show 2>/dev/null)"; then
    echo
    warn "本机内存 ${ram}MB 且无 swap, 跑代理建议加一个：$(disp) harden --swap 2G"
  fi
}

# ── 系统加固 ────────────────────────────────────────────────────────────────
# 与网络参数无关, 但小内存机不加 swap 就没有任何缓冲余地：TCP 缓冲区一涨,
# 内核直接杀进程. 表现是"测速跑一半掉速", 要翻 journalctl 才看得出来.
cmd_harden(){
  need_root
  take_lock
  local swap_size=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --swap) swap_size="$2"; shift 2 ;;
      *) die "未知参数: $1" ;;
    esac
  done
  [ -n "$swap_size" ] || die "需要 --swap <大小>, 例如 --swap 2G 或 --swap 2"

  # 单位一律按 GB. "2" 和 "2G" 都收 —— 向导和文档里写的都是 2G, 只收纯数字
  # 会把它们全挡掉（v0.4.3 就是这样, 向导结尾的 swap 提示按 y 之后直接 die 退出）.
  # 但不收 "2M": fallocate 会建 2MB, 而失败回退的 dd 建 2GB, 两条路差 1000 倍.
  local gb="${swap_size%[Gg]}"
  is_posint "$gb" 1 20 || die "swap 大小请填 1-20 之间的整数, 单位 GB（例如 2 或 2G）"
  swap_size="${gb}G"

  # 校验通过再存快照, 打错参数不该留下状态
  take_snapshot        # harden 会往 $SYSCTL_FILE 追加 vm.swappiness,
                       # 不存快照的话之后跑 tune 会因"有配置无快照"直接中止

  if not_blank "$(swapon --show 2>/dev/null)"; then
    info "已有 swap, 跳过: $(free -h | awk '/Swap/{print $2}')"
    return 0
  fi
  # 已存在但没启用的 /swapfile 不能盖 —— 那可能是用户自己准备的
  [ -e /swapfile ] && die "/swapfile 已存在但未启用. 先确认它的用途, 需要的话手动删除后再跑"
  info "创建 ${swap_size} swap…"
  fallocate -l "$swap_size" /swapfile 2>/dev/null \
    || dd if=/dev/zero of=/swapfile bs=1M count=$(( gb * 1024 )) status=none
  chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile \
    || die "swap 创建失败"
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  # 记下这个 swapfile 是 tcpfit 建的; --purge-swap 只删有这个标记的
  mkdir -p "$STATE_DIR"; : > "$STATE_DIR/swapfile.owned"
  ok "swap 已启用: $(free -h | awk '/Swap/{print $2}')"
  # 只在内存真的紧张时才用 swap, 避免平时把热数据换出去拖慢代理
  sysctl -qw vm.swappiness=10
  grep -q '^vm.swappiness' "$SYSCTL_FILE" 2>/dev/null || echo "vm.swappiness = 10" >> "$SYSCTL_FILE"
  ok "vm.swappiness = 10"
}

# ── 出向整形 ────────────────────────────────────────────────────────────────
# HTB 做全局上限（多流场景必需）, fq 叶子做 hrtimer 逐包 pacing.
# burst 取 4ms 的线速数据量：避免高带宽下 32k 太浅, 同时仍限制微突发.
qdisc_root_kind(){   # qdisc_root_kind <iface>
  local out
  out=$(tc qdisc show dev "$1" 2>/dev/null)
  awk '$1=="qdisc"{for(i=1;i<=NF;i++) if($i=="root"){print $2; exit}}' <<<"$out"
}

# 内核自动创建的多队列根 qdisc 常显示为 `mq 0:`. handle 0 不能直接删除，
# 会报 "Cannot delete qdisc with handle of zero". 先把同一个 mq 换成普通句柄，
# 再删除；其他 qdisc 仍走一次普通 del.
qdisc_remove_root(){   # qdisc_remove_root <iface>
  local iface="$1" handle
  tc qdisc del dev "$iface" root 2>/dev/null && return 0
  # 删根失败有三种情况, 早期版本把后两种一律当失败:
  #   a) 真失败
  #   b) 根是 mq 且句柄为 0 —— 先给它一个真句柄才删得掉
  #   c) 根是内核开机自动装的默认 qdisc, 句柄就是 0 ——
  #      它【删不掉, 但能被直接覆盖】. 实测句柄 0 的根上 add / replace 都 rc=0.
  # 把 c 当失败的代价: 一台刚开机、default_qdisc 还是 fq_codel 的机器
  # （Ubuntu/Debian 出厂就是这样）跑 sweep 会停在
  # "failed to enable fq for unshaped probe", 而它其实什么毛病都没有.
  if [ "$(qdisc_root_kind "$iface")" = mq ]; then
    tc qdisc replace dev "$iface" root handle 1: mq 2>/dev/null || return 1
    tc qdisc del dev "$iface" root 2>/dev/null
    return $?
  fi
  handle=$(tc qdisc show dev "$iface" 2>/dev/null |
           awk '$1=="qdisc"{for(i=1;i<=NF;i++) if($i=="root"){print $3; exit}}')
  # 句柄 0 = 内核自己装的, 没什么可删, 交给调用方覆盖
  [ "$handle" = "0:" ] && return 0
  return 1
}

qdisc_set_mq_leaves(){   # qdisc_set_mq_leaves <iface> <kind>
  local iface="$1" kind="$2" out handle major parents p
  out=$(tc qdisc show dev "$iface" 2>/dev/null)
  handle=$(awk '$1=="qdisc" && $2=="mq"{
    for(i=1;i<=NF;i++) if($i=="root"){print $3; exit}}' <<<"$out")
  major=${handle%:}
  # 内核自建的 mq 句柄是 `0:`, 它的叶子在 tc 里显示成 `parent :N`,
  # 但那个写法【无法用来寻址】—— 真机实测(104.250, 8 队列 10G):
  #   tc qdisc replace dev eth0 parent :1  fq  →  Error: Failed to find specified qdisc.
  #   tc qdisc replace dev eth0 parent 0:1 fq  →  同样失败
  # 而真网卡开机后的默认状态就是 `mq 0:`, 所以不先给句柄的话这个函数在实机上必定失败.
  # 先 `replace root handle 1: mq`(只改句柄, 叶子原样保留), 叶子变成 `parent 1:N` 才能换.
  if [ -z "$major" ] || [ "$major" = 0 ]; then
    tc qdisc replace dev "$iface" root handle 1: mq 2>/dev/null || return 1
    out=$(tc qdisc show dev "$iface" 2>/dev/null)
    handle=$(awk '$1=="qdisc" && $2=="mq"{
      for(i=1;i<=NF;i++) if($i=="root"){print $3; exit}}' <<<"$out")
    major=${handle%:}
  fi
  [ -n "$major" ] || return 1
  parents=$(awk -v m="$major" '$1=="qdisc" && $0 ~ / parent /{
    for(i=1;i<=NF;i++) if($i=="parent"){
      p=$(i+1)
      if((m=="0" && (p ~ /^:/ || index(p,"0:")==1)) || (m!="0" && index(p,m ":")==1)) print p
      break}}
  ' <<<"$out")
  [ -n "$parents" ] || return 1
  for p in $parents; do
    tc qdisc replace dev "$iface" parent "$p" "$kind" 2>/dev/null || return 1
  done
}

qdisc_is_fq(){   # root fq，或 mq 下所有硬件队列均为 fq
  local out root handle major
  out=$(tc qdisc show dev "$1" 2>/dev/null)
  root=$(awk '$1=="qdisc"{for(i=1;i<=NF;i++) if($i=="root"){print $2; exit}}' <<<"$out")
  [ "$root" = fq ] && return 0
  [ "$root" = mq ] || return 1
  handle=$(awk '$1=="qdisc" && $2=="mq"{
    for(i=1;i<=NF;i++) if($i=="root"){print $3; exit}}' <<<"$out")
  major=${handle%:}
  [ -n "$major" ] || return 1
  awk -v m="$major" 'BEGIN{leaves=0; bad=0}
       $1=="qdisc" && $0 ~ / parent /{
         for(i=1;i<=NF;i++) if($i=="parent"){
           p=$(i+1)
           if((m=="0" && (p ~ /^:/ || index(p,"0:")==1)) || (m!="0" && index(p,m ":")==1)){
             leaves++; if($2!="fq") bad=1
           }
           break
         }
       }
       END{exit !(leaves>0 && bad==0)}' <<<"$out"
}

# mq 是网卡的硬件多队列结构，不能为了启用 fq 把它压成单队列 root fq.
# 保留 mq 根，只替换每个叶子；普通网卡则安装 root fq.
qdisc_set_fq(){   # qdisc_set_fq <iface>
  local iface="$1" kind
  kind=$(qdisc_root_kind "$iface")
  case "$kind" in
    mq) qdisc_set_mq_leaves "$iface" fq || return 1 ;;
    fq) : ;;
    ""|noqueue) tc qdisc add dev "$iface" root fq 2>/dev/null || return 1 ;;
    *) qdisc_remove_root "$iface" || return 1
       # 删掉非 fq 的根之后【要重新看一眼内核装了什么】.
       # 多队列网卡上内核会自动补回 mq + 每队列的 default_qdisc ——
       # 真机实测(104.250, 8 队列 10G): 装上 HTB 再 `tc qdisc del root`,
       # 立刻变成 `qdisc mq 0: root` + 8 个 fq 叶子.
       # 这时候直接 `add root fq` 会把 mq 压成单根 fq, 8 个硬件队列退化成一把锁 ——
       # 正是本版修 mq 想避免的事. cmd_shape --off 走的就是这条分支.
       kind=$(qdisc_root_kind "$iface")
       case "$kind" in
         mq) qdisc_set_mq_leaves "$iface" fq || return 1 ;;
         fq) : ;;
         *)  tc qdisc replace dev "$iface" root fq 2>/dev/null || return 1 ;;
       esac ;;
  esac
  qdisc_is_fq "$iface"
}

write_qdisc(){
  local rate="$1" iface="$2"
  cat > "$QDISC_SCRIPT" <<EOF
#!/bin/bash
# 网卡名动态探测. 写死的话, 服务商换宿主/换槽位导致网卡改名之后
# 脚本就一直 "Cannot find device" —— 用户只看到服务 failed, 看不出原因.
# 探测不到才回退到生成时的名字.
IF=\$(ip -o -4 route show default 2>/dev/null | awk '{print \$5; exit}')
[ -n "\$IF" ] || IF=${iface}
RATE=\${1:-${rate}}
BURST=\$(awk -v r="\$RATE" 'BEGIN{v=r*500; if(v<32768)v=32768; printf "%d",v}')
# 删根失败有两种可能, 早期版本把它们混为一谈:
#   a) 本来就没有可删的根（开机时最常见, 内核补回的默认 qdisc 句柄是 0）
#   b) 根是 mq 且句柄为 0 —— 要先给它一个真句柄才删得掉
# 无脑走 (b) 的话, 单队列网卡上 \`tc qdisc replace ... root mq\` 会报
# "RTNETLINK answers: Operation not supported" 然后 exit 1,
# 于是 tcpfit-qdisc.service 每次开机都失败, 整形静默丢失（issue #7）.
if ! tc qdisc del dev \$IF root 2>/dev/null; then
  case "\$(tc qdisc show dev \$IF 2>/dev/null | head -1)" in
    *" mq "*) tc qdisc replace dev \$IF root handle 1: mq 2>/dev/null &&
              tc qdisc del dev \$IF root 2>/dev/null ;;
  esac
fi
# 一律用 replace 而不是 add —— add 在已有同句柄 qdisc 时报 "File exists",
# 让重复执行(手动重跑 / systemd 重启单元)变成失败.
tc qdisc replace dev \$IF root handle 1: htb default 10 || exit 1
tc class replace dev \$IF parent 1: classid 1:10 htb rate \${RATE}mbit ceil \${RATE}mbit burst \${BURST} cburst \${BURST} quantum 1514 || exit 1
tc qdisc replace dev \$IF parent 1:10 handle 10: fq limit 40960 flow_limit 8192 maxrate \${RATE}mbit || exit 1
EOF
  chmod +x "$QDISC_SCRIPT"
  cat > "$QDISC_UNIT" <<EOF
[Unit]
Description=tcpfit egress shaper
After=network-online.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$QDISC_SCRIPT $rate
[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload >/dev/null 2>&1
  # 用 --now 而不是只 enable：否则 tc 规则虽已生效, systemctl is-active 却显示
  # inactive, status 里看着像坏了. 让 unit 状态和实际状态一致.
  systemctl enable --now tcpfit-qdisc.service >/dev/null 2>&1
}

cmd_shape(){
  need_root
  take_lock
  local rate="" off=0 iface
  while [ $# -gt 0 ]; do
    case "$1" in
      --rate) rate="$2"; shift 2 ;;
      --off)  off=1; shift ;;
      *) die "未知参数: $1" ;;
    esac
  done
  iface=$(detect_iface)

  # 没有限速器可躲的机器（sweep 全程干净）, HTB 的硬上限只会限制自己 ——
  # iperf3 的 TCP payload 通常只有线速的 93-96%（协议开销, 不是 HTB 损耗）,
  # 没有上游 policer 时保留聚合硬上限没有收益.
  # 移除后根 qdisc 退回纯 fq：BBR 的逐 socket pacing 仍然生效, 只是没有聚合上限.
  # 注意不动 sysctl, 基础调优完整保留 —— 这是它和 rollback 的区别.
  if [ "$off" = 1 ]; then
    systemctl disable --now tcpfit-qdisc.service >/dev/null 2>&1
    rm -f "$QDISC_UNIT" "$QDISC_SCRIPT"
    systemctl daemon-reload >/dev/null 2>&1
    qdisc_set_fq "$iface" || { warn "整形已移除，但无法在 ${iface} 上启用 fq"; return 1; }
    ok "整形已移除, qdisc 恢复为纯 fq"
    info "BBR 的逐 socket pacing 仍然生效, 只是没有了聚合速率上限."
    info "基础调优（拥塞控制 / 缓冲区）未受影响."
    [ "$WIZARD" = 1 ] || tc qdisc show dev "$iface" | head -1
    return 0
  fi

  [ -n "$rate" ] || die "需要 --rate <Mbit>, 或用 --off 移除整形"
  # 校验必须在 take_snapshot 之前 —— 否则打错一个字就会留下快照和半截 qdisc
  is_posint "$rate" 1 100000 || die "--rate 必须是 1-100000 的整数（Mbit）"
  take_snapshot
  write_qdisc "$rate" "$iface"
  systemctl restart tcpfit-qdisc.service 2>/dev/null || "$QDISC_SCRIPT" "$rate"
  # 事后用 tc 核对, 不能只看命令有没有报错.
  # 【不能】直接 grep "rate ${rate}Mbit" —— tc 对除得尽的值会换成 Gbit 显示:
  #   --rate 500 → "rate 500Mbit"   --rate 1000 → "rate 1Gbit"
  #   --rate 5000 → "rate 5Gbit"    --rate 9171 → "rate 9171Mbit"(除不尽, 不换)
  # 于是整千的整形值会匹配失败, 整形明明生效却报
  # "shaping did not take effect" —— 而 1000/2000/10000 正是最常手输的值.
  # 改成读回来换算成 Mbit 再比, 留 1% 容差(tc 内部按字节/秒存, 换算有舍入).
  local applied
  applied=$(tc_rate_mbit "$(tc class show dev "$iface" 2>/dev/null)")
  if [ -n "$applied" ] && awk -v a="$applied" -v r="$rate" \
       'BEGIN{exit !(a > r*0.99 && a < r*1.01)}' 2>/dev/null; then
    ok "HTB ${rate} Mbit + fq leaf pacing on ${iface}"
  else
    warn "shaping did not take effect on ${iface} -- check: tc qdisc show dev ${iface}"
    return 1
  fi
  systemctl is-enabled tcpfit-qdisc.service >/dev/null 2>&1 \
    && ok "tcpfit-qdisc.service enabled (survives reboot)" \
    || warn "tcpfit-qdisc.service not enabled -- shaping will be lost on reboot"
  [ "$WIZARD" = 1 ] || tc class show dev "$iface"
}

# 测试期间的临时整形. 结构必须和 write_qdisc 生成的完全一致.
#
# 两个原因:
#   1) fq 的 maxrate 是【每条流】的上限, 不是聚合上限. 实测: fq maxrate 300mbit
#      跑 -P 1 得 283 Mbps, 跑 -P 4 得 1134 Mbps(约 4 倍). 只有 HTB 才是聚合限速.
#      早期版本用 fq maxrate 做限速, 于是 validate_peer(跑 -P 2)名义上限 40%
#      实际能冲到 80%, 可能撞上限速器再把丢包报成"链路本身有损".
#   2) 扫描用一种结构、最终应用另一种结构的话, 测出来的拐点对不上实际部署.
apply_test_shaper(){   # apply_test_shaper <iface> <rate_mbit>
  local iface="$1" rate="$2" burst
  burst=$(calc_burst "$rate")
  qdisc_remove_root "$iface" || return 1
  tc qdisc add dev "$iface" root handle 1: htb default 10 2>/dev/null || return 1
  tc class add dev "$iface" parent 1: classid 1:10 htb \
     rate "${rate}mbit" ceil "${rate}mbit" burst "$burst" cburst "$burst" quantum 1514 2>/dev/null || return 1
  tc qdisc add dev "$iface" parent 1:10 handle 10: fq \
     limit 40960 flow_limit 8192 maxrate "${rate}mbit" 2>/dev/null || return 1
}

# ── 测试用 qdisc 的保存与恢复 ────────────────────────────────────────────────
# probe / validate_peer / sweep 都要临时换掉根 qdisc. 早期版本恢复时一律装成 fq,
# 于是原来的 mq(多队列网卡的正常结构)、CAKE 等配置被永久吞掉且无提示.
# 现在完整记下原始根 qdisc, 结束时按原样恢复.
QSAVE_KIND=""; QSAVE_LEAF_KIND=""; QSAVE_IFACE=""
qdisc_save(){   # qdisc_save <iface>
  local out handle major
  QSAVE_IFACE="$1"
  out=$(tc qdisc show dev "$1" 2>/dev/null)
  QSAVE_KIND=$(awk '$1=="qdisc"{for(i=1;i<=NF;i++) if($i=="root"){print $2; exit}}' <<<"$out")
  QSAVE_LEAF_KIND=""
  if [ "$QSAVE_KIND" = mq ]; then
    handle=$(awk '$1=="qdisc" && $2=="mq"{
      for(i=1;i<=NF;i++) if($i=="root"){print $3; exit}}' <<<"$out")
    major=${handle%:}
    QSAVE_LEAF_KIND=$(awk -v m="$major" '$1=="qdisc" && $0 ~ / parent /{
      for(i=1;i<=NF;i++) if($i=="parent"){
        p=$(i+1)
        if((m=="0" && (p ~ /^:/ || index(p,"0:")==1)) || (m!="0" && index(p,m ":")==1)){print $2; exit}
        break}}
    ' <<<"$out")
  fi
}
# 把本脚本起的 iperf3 全部收掉. 只杀自己的子进程, 不动用户手工跑的.
# BusyBox(Alpine) 的 timeout 没有 --foreground, pkill 没有 -g. 启动时探一次,
# 不支持就退回到能用的写法, 而不是让每次调用都报错.
TIMEOUT_FG=""
timeout --foreground 1 true >/dev/null 2>&1 && TIMEOUT_FG="--foreground"
# 不能靠"跑一次看退出码"判断: BusyBox 不认 -g 时也返回 1, 会被误判成支持.
# 改看帮助里有没有长选项 --pgroup —— BusyBox 压根不支持长选项.
PKILL_G=0
has_str "$(pkill --help 2>&1)" "--pgroup" && PKILL_G=1

# 收掉本脚本起的 iperf3. 优先按进程组匹配 —— iperf3 的父进程是 timeout 不是本脚本,
# 按 -P $$ 匹配不到. 只杀同组的, 不动用户手工跑的.
reap_iperf(){
  if [ "$PKILL_G" = 1 ]; then
    pkill -g $$ -x iperf3 2>/dev/null; pkill -g $$ -x timeout 2>/dev/null
  else
    pkill -P $$ -x iperf3 2>/dev/null; pkill -P $$ -x timeout 2>/dev/null
  fi
  return 0
}

qdisc_restore(){
  reap_iperf
  [ -n "$QSAVE_IFACE" ] || return 0
  qdisc_remove_root "$QSAVE_IFACE" 2>/dev/null || true
  if [ -x "$QDISC_SCRIPT" ]; then
    "$QDISC_SCRIPT" >/dev/null 2>&1 && return 0   # 同理, 不清 QSAVE_IFACE
  fi
  case "$QSAVE_KIND" in
    mq)
      # 有些驱动删除临时 HTB 后会自动重建 mq，有些不会；两种都兼容.
      [ "$(qdisc_root_kind "$QSAVE_IFACE")" = mq ] || \
        tc qdisc add dev "$QSAVE_IFACE" root mq 2>/dev/null || return 1
      [ -z "$QSAVE_LEAF_KIND" ] || \
        qdisc_set_mq_leaves "$QSAVE_IFACE" "$QSAVE_LEAF_KIND" 2>/dev/null || return 1
      ;;
    ""|noqueue|pfifo_fast) : ;;
    *) tc qdisc add dev "$QSAVE_IFACE" root "$QSAVE_KIND" 2>/dev/null ;;
  esac
  # ⚠ 这里【不能】清空 QSAVE_IFACE.
  # cmd_sweep 会调它两次: 不限速探测结束后一次, 扫描全部结束后一次.
  # 早期版本在这里清空, 于是第二次调用直接 return 0 什么都不做 ——
  # 扫描最后一档的 HTB 就留在网卡上, 而屏幕上还打印 "qdisc restored".
  # 实测: 香港 CN2 跑完 sweep 后机器上仍挂着 class htb rate 31Mbit.
  # 只在"扫描跑了但没定位到拐点"时暴露（找到拐点的话后面 cmd_shape 会覆盖掉）.
  # del + add 本身是幂等的, 重复调用无害, 所以不需要这个哨兵.
}
# 未知/自定义 qdisc 不是我们能原样重建的, 先问过用户
qdisc_guard(){   # qdisc_guard <iface>
  local k; k=$(qdisc_root_kind "$1")
  case "$k" in
    ""|mq|fq|noqueue|pfifo_fast|fq_codel|htb) return 0 ;;
  esac
  warn "本机根 qdisc 是 ${k}, 测试期间会被临时替换."
  warn "结束时只能恢复成 ${k} 的默认参数, 自己的调优配置会丢失."
  confirm "  继续？" || return 1
}

# ── 带宽探测 ────────────────────────────────────────────────────────────────
# 虚拟网卡读不到标称速率(/sys/class/net/*/speed 为 -1), 而用户未必记得买的是多少兆.
# 这里用带 pacing 的多流测试估一个可用带宽, 供 tune 推导 BDP.
# 注意：这只是"够用的估计", 真正的限速器拐点仍要靠 sweep 实测.
probe_bandwidth(){
  local peer="$1" iface="$2" dur="${3:-10}"
  qdisc_save "$iface"
  trap 'qdisc_restore; exit 130' INT TERM HUP
  # 用 fq 做 pacing 但不设上限: 既避免突发打穿限速器, 又能探到真实上限
  qdisc_set_fq "$iface" || { qdisc_restore; echo ""; return 1; }
  local bw
  bw=$(measure_bandwidth "$peer" "$dur")
  trap - INT TERM HUP
  qdisc_restore
  [ -n "$bw" ] || return 1
  printf '%s' "$bw"
}

# 国际线路调优使用四连接探测与取整；优化线路调优仅在两端标称带宽留空时由协调模块探测。
measure_bandwidth(){
  local peer="$1" dur="${2:-10}" res="" gp a
  for a in 1 2 3; do res=$(run_iperf "$peer" "$dur" 4); [ -n "$res" ] && break; sleep 8; done
  [ -n "$res" ] || { echo ""; return 1; }
  # run_iperf 第三列是接收端实际送达量. 老版 iperf3 没给 receiver 汇总时
  # 第三列为空, 退回使用既有的发送端数字.
  gp=$(echo "$res" | awk '{print $3}')
  [ -n "$gp" ] || gp=$(echo "$res" | awk '{print $1}')
  # 取整粒度跟档位走. 早期版本无脑向上取整, 实测把 305Mbps 估成 350,
  # 导致 sweep 的扫描区间整体偏高 —— 所以要取整.
  # 但粒度不能一刀切 50: int(10/50+0.5)*50 = 0, 十兆小水管直接归零,
  # 向导会打印 "Measured ~0 Mbps" 然后 die "无法确定带宽". 客户实际踩过.
  # 归零阈值是 25 Mbps, 25-49 还会被高估最多一倍(27.8→50, 而真实容量约 15).
  awk -v g="$gp" 'BEGIN{
    if      (g < 50)  s = 1      # 小水管: 不取整, 差 1M 都是差
    else if (g < 200) s = 10
    else              s = 50     # 大机器: 保持原行为(305→300, 481→500)
    printf "%d", int(g/s+0.5)*s }'
}

cmd_probe(){
  need_root
  take_lock
  command -v iperf3 >/dev/null || die "需要 iperf3"
  local peer=""
  while [ $# -gt 0 ]; do
    case "$1" in --peer) peer="$2"; shift 2 ;; *) die "未知参数: $1" ;; esac
  done
  [ -n "$peer" ] || die "需要 --peer <近处的iperf3服务器>"
  local iface; iface=$(detect_iface)
  qdisc_guard "$iface" || { info "已取消"; return 0; }
  info "探测可用带宽（4 并发 + pacing, 约 15 秒）…"
  local bw; bw=$(probe_bandwidth "$peer" "$iface")
  [ -n "$bw" ] || die "探测失败, 检查对端 $peer 是否可达/空闲" 2
  mkdir -p "$STATE_DIR"; echo "BW_MBPS=$bw" > "$STATE_DIR/probe.result"
  ok "估计可用带宽 ≈ ${bw} Mbps"
  echo
  echo "  这只是给 tune 算 BDP 用的估计值, 真正的限速器拐点靠 sweep 实测."
  echo "  下一步: $0 tune --role <proxy|bulk|mixed> --bw $bw"
}

# ── 限速器拐点扫描 ──────────────────────────────────────────────────────────
# 原理: 端口上的限速器(policer)看的是瞬时速率. 不加 pacing 的 TCP 发送是突发的,
# 平均速率没超也会被打穿. 加 fq pacing 后可以贴着真实上限跑而几乎不丢包.
# 拐点 = 重传开始跳变的那一档；取前一档再退安全余量.
# NETTUNE_VERBOSE=1 时把 iperf3 原始输出打到 stderr, 让用户看到测速在跑
# $1=peer $2=dur $3=parallel [$4=port]  -> "sender_mbps retrans [receiver_mbps]"
# 前两列是既有契约, 第三列只追加不改义；没有 receiver 汇总时仍返回两列.
# 公共节点各开十个实例（Leaseweb/OVH 5201-5210, Clouvider 5200-5209）,
# 指定端口忙时自动换 —— 否则单端口一忙就整个失败. 端口表见 PORT_POOL.
run_iperf(){
  if [ -n "${TCPFIT_RETURN_RUN:-}" ]; then
    python3 "$TCPFIT_RETURN_HELPER" measure --run-dir "$TCPFIT_RETURN_RUN" \
      --duration "$2" --streams "$3" --stage "$TCPFIT_RETURN_STAGE"
    return $?
  fi
  local out recv raw tmp port ports pid sg rt rg="" first="${4:-${PEER_PORT:-5201}}"
  ports=$(port_order "$first")
  tmp=$(mktemp)
  for port in $ports; do
    : > "$tmp"
    timeout $TIMEOUT_FG $(( $2 + 25 )) iperf3 $IP_FAMILY -c "$1" -p "$port" -t "$2" -P "$3" -f m >"$tmp" 2>&1 &
    pid=$!
    # --foreground 是必须的: timeout 默认把子进程放进【独立进程组】(方便超时时杀整组),
    # 结果 Ctrl-C 发给脚本进程组的 SIGINT 根本到不了 iperf3, 它会继续满速跑到
    # timeout 到期 —— 9Gbps 的机器上那是十几 GB 白烧. 实测验证过:
    #   默认        iperf3 进程组 ≠ 脚本组, Ctrl-C 后残留 1 个
    #   --foreground 两者相同,        Ctrl-C 后残留 0 个
    # 下面的 trap 是第二道保险, 走 kill 路径时用.
    trap 'kill -TERM "$pid" 2>/dev/null; pkill -P "$pid" 2>/dev/null; rm -f "$tmp"; exit 130' INT TERM HUP
    spin_wait "$pid" "测速中… ${2}s × ${3} 流  →  $1:$port"
    trap - INT TERM HUP
    # 判据是"有没有拿到有效结果", 不是枚举报错文案 —— iperf3 在服务端忙的时候
    # 会随机吐两种错, 早期只认 "busy running a test", 碰上
    # "unable to send control message: Connection reset by peer" 就直接放弃换端口了.
    grep -qE "$( [ "$3" -gt 1 ] && echo 'SUM.*sender' || echo 'sender' )" "$tmp" 2>/dev/null && break
  done
  raw=$(cat "$tmp"); rm -f "$tmp"
  [ "${NETTUNE_VERBOSE:-0}" = 1 ] && echo "$raw" | sed 's/^/      | /' >&2
  out=$(echo "$raw" | grep -E "$( [ "$3" -gt 1 ] && echo 'SUM.*sender' || echo 'sender' )" | tail -1)
  [ -z "$out" ] && { echo ""; return; }
  recv=$(echo "$raw" | grep -E "$( [ "$3" -gt 1 ] && echo 'SUM.*receiver' || echo 'receiver' )" | tail -1)
  sg=$(echo "$out"  | awk '{print $(NF-3)}')
  rt=$(echo "$out"  | awk '{print $(NF-1)}')
  [ -n "$recv" ] && rg=$(echo "$recv" | awk '{print $(NF-2)}')
  if [ -n "$rg" ]; then printf '%s %s %s\n' "$sg" "$rt" "$rg"
  else                     printf '%s %s\n'    "$sg" "$rt"; fi
}

# 估算重传比(%) = 重传次数 / 按 1448 字节 MSS 估算的发送段数。
# 这是沿用历史阈值的比较指标，不是实际丢包率；实际重传次数单独展示。
#
# 为什么不能用绝对次数：阈值 100 在 300M 机上相当于 0.032% 丢包,
# 在 7.4G 机上只有 0.0014% —— 严了 25 倍. 实测踩过：一台 10G 口的机器
# 第一档 7440Mbit 实测 7001Mbps、重传 101（丢包率 0.0014%, 链路干净得离谱）,
# 却被判成撞了限速器, LAST_OK 为空直接报 "no usable rate measured" 退出,
# 整个扫描一档都没跑成.
#
# 七组真实数据回归：干净侧最高 0.0017%, 撞限速器最低 1.3541%.
# 阈值取 0.1%, 距两侧分别有 59 倍和 13.5 倍余量, 且自动适配任何带宽.
loss_pct(){   # loss_pct <重传数> <吞吐Mbps> <秒数>
  awk -v rt="$1" -v gp="$2" -v d="$3" 'BEGIN{
    pk = gp*1000000*d/8/1448          # 发出的包数
    if(pk < 1) pk = 1
    printf "%.4f", rt*100/pk
  }'
}

# 国际线路调优和公共节点验证使用以下判据；优化线路调优使用独立的单连接重传阈值。
RETRANS_THRESHOLD=0.1
VERIFY_GOOD_PCT=90
VERIFY_ACCEPT_PCT=75
retrans_is_spike(){
  awk -v l="$1" -v b="$2" -v t="${3:-$RETRANS_THRESHOLD}" 'BEGIN{
    need=t; if(b>0 && b*5>need) need=b*5; if(need>1) need=1
    exit !(l>need)
  }'
}
retrans_band(){
  awk -v l="$1" 'BEGIN{if(l<0.05) print 0; else if(l<0.5) print 1; else if(l<1) print 2; else print 3}'
}

cmd_sweep(){
  need_root
  take_lock
  command -v iperf3 >/dev/null || die "需要 iperf3: apt install -y iperf3 / yum install -y iperf3"
  # GAP: 档与档之间的静置时间, 让上一条流的状态排空, 避免相邻两档互相干扰
  local peer="" nominal="" lo="" hi="" step="" dur=12 par=1 margin="" thresh="$RETRANS_THRESHOLD" refine=1 GAP=3 cap=10000
  local PRE_SCAN_GAP=15 BASELINE_CAP=0.5
  # cap 和 AGG_MIN 是两件事, 早期版本共用一个值, 抬 cap 会连带改掉"多大算大机器":
  #   cap     —— 愿意扫到多高（--cap 可调）
  #   AGG_MIN —— 单流不可信、要用 8 流复核的带宽门槛（跟 cap 无关）
  local AGG_MIN=2500
  while [ $# -gt 0 ]; do
    case "$1" in
      --peer) peer="$2"; shift 2 ;;
      --port) PEER_PORT="$2"; shift 2 ;;
      -4) IP_FAMILY="-4"; shift ;;
      -6) IP_FAMILY="-6"; shift ;;
      --nominal) nominal="$2"; shift 2 ;;
      --from) lo="$2"; shift 2 ;;
      --to) hi="$2"; shift 2 ;;
      --step) step="$2"; shift 2 ;;
      --dur) dur="$2"; shift 2 ;;
      --parallel) par="$2"; shift 2 ;;
      --margin) margin="$2"; shift 2 ;;
      --gap) GAP="$2"; shift 2 ;;
      --cap) cap="$2"; shift 2 ;;
      --no-refine) refine=0; shift ;;
      --loss-threshold|--retrans-threshold) thresh="$2"; shift 2 ;;   # 单位是百分比
      *) die "未知参数: $1" ;;
    esac
  done
  for _v in "nominal:$nominal:1:1000000" "step:$step:1:100000" "dur:$dur:1:600" \
            "par:$par:1:128" "lo:$lo:1:1000000" "hi:$hi:1:1000000" "gap:$GAP:0:60"; do
    _n=${_v%%:*}; _r=${_v#*:}; _val=${_r%%:*}; _r=${_r#*:}; _min=${_r%%:*}; _max=${_r#*:}
    [ -z "$_val" ] && continue
    is_posint "$_val" "$_min" "$_max" || die "--${_n} 必须是 ${_min}-${_max} 的整数"
  done
  [ -n "$peer" ] || die "需要 --peer <iperf3服务器>, 选延迟低的, 测的是本机端口上限而非跨国链路"
  local iface; iface=$(detect_iface)
  # 手工给了区间就完全按用户说的来, 不做不限速探测
  local user_range=""; [ -n "$lo" ] && [ -n "$hi" ] && user_range=1
  # 手工给了区间: 缺的标称值用区间上界顶上. 自动模式下 nominal/lo/hi/step
  # 全部由后面的不限速探测决定, 这里不需要它们.
  if [ -n "$user_range" ]; then
    [ -n "$nominal" ] || nominal="$hi"
    [ -n "$step" ] || step=$(calc_step "$nominal")
  fi
  is_posint "$cap" 100 100000 || die "--cap 必须是 100-100000 的整数"
  # 校验一过就清掉上一轮的结果, 必须在任何 return 之前 ——
  # 否则这轮失败(对端太慢/探测失败/取消)时, 向导和菜单会读到上次的 RECOMMEND
  # 并把旧限速值应用上去, 而屏幕上写的是 "shaping skipped".
  mkdir -p "$STATE_DIR"; rm -f "$STATE_DIR/sweep.result"
  qdisc_guard "$iface" || { info "已取消"; return 0; }

  [ "$WIZARD" = 1 ] || traffic_mark
  info "Peer ${peer}:${PEER_PORT}"

  # 扫描会反复替换 qdisc；无论正常结束、拐点 break 还是被 Ctrl-C,
  # 都必须把机器恢复原状 —— 否则会被留在那个暴丢包的档位上.
  qdisc_save "$iface"
  restore_qdisc(){ qdisc_restore; info "qdisc restored"; }
  trap 'echo; warn "interrupted, restoring qdisc..."; qdisc_restore; exit 130' INT TERM HUP   # 中断退出是对的

  # 扫一段区间. 结果放进全局 LAST_OK(最后一个干净档) 与 BROKE_AT(重传跳变的那档)
  LAST_OK=""; BROKE_AT=""; SLOW_HITS=0; PEER_TOO_SLOW=0; BASE_LOSS=""; SPIKE_MIN_LOSS=""; SLOW_AT=""
  # 跳变判定: 既要超过绝对阈值, 也要明显高于本底. 远程对端可能有
  # 0.1%-0.3% 的稳定底噪, 所以用 5 倍本底; 同时把相对阈值封顶在 1%,
  # 避免底噪把实测 1.35% 以上的 policer 拐点完全遮住.
  is_spike(){
    retrans_is_spike "$1" "${BASE_LOSS:-0}" "$thresh"
  }
  scan_range(){
    local a b st r res sgp gp rt lp prev_gp=0 verdict
    a=$1; b=$2; st=$3
    # 终点必测: 168→221 步长 20 只会测 168/188/208, 而提示里写的是"扫到 221",
    # 上界从来没被验证过. 补一档把终点带上.
    local pts="" _r
    for (( _r=a; _r<=b; _r+=st )); do pts="$pts $_r"; done
    case " $pts " in *" $b "*) ;; *) pts="$pts $b" ;; esac
    for r in $pts; do
      apply_test_shaper "$iface" "$r" || { warn "failed to apply test shaper at ${r} Mbit"; return 1; }
      res=""
      # 进度提示交给 run_iperf 里的转圈, 这里不要再打占位符（会和转圈重叠）
      for _ in 1 2 3; do res=$(run_iperf "$peer" "$dur" "$par"); [ -n "$res" ] && break; sleep 8; done
      if [ -z "$res" ]; then printf '  %-10s %12s %9s %8s  %s\n' "$r" "-" "-" "-" "peer busy, skipped"; continue; fi
      sgp=$(echo "$res" | awk '{print $1}'); rt=$(echo "$res" | awk '{print $2}')
      gp=$(echo "$res" | awk '{print $3}'); [ -n "$gp" ] || gp="$sgp"
      lp=$(loss_pct "$rt" "$sgp" "$dur")
      verdict="ok"
      # 只有干净样本才能建立本底. 不限速探测刚打穿 policer 时,
      # 第一档可能带 0.4%-8% 的假丢包; 把它当基线后再要求 10 倍跳变,
      # 真实拐点就永远触发不了. 首个超阈值样本必须按疑似跳变复测.
      if [ -z "$BASE_LOSS" ] && awk -v l="$lp" -v t="$thresh" 'BEGIN{exit !(l <= t)}'; then
        BASE_LOSS="$lp"
      fi
      # 判定用丢包率而非绝对重传数 —— 见 loss_pct 上方注释.
      # 单次跳变可能只是公共节点被别人占用, 所以要复测: 3 次里 ≥2 次跳变才确认.
      if is_spike "$lp"; then
        local hits=1 j clean_gp="" clean_rt="" clean_lp=""
        local min_lp="$lp"
        for j in 2 3; do
          sleep "$GAP"
          local r2 s2 g2 t2 l2
          r2=$(run_iperf "$peer" "$dur" "$par"); [ -z "$r2" ] && continue
          s2=$(echo "$r2" | awk '{print $1}'); t2=$(echo "$r2" | awk '{print $2}')
          g2=$(echo "$r2" | awk '{print $3}'); [ -n "$g2" ] || g2="$s2"
          l2=$(loss_pct "$t2" "$s2" "$dur")
          printf '  %-10s %12s %9s %8s  %s\n' "${r} (#${j})" "$g2" "$t2" "$l2" "recheck"
          if awk -v x="$l2" -v y="$min_lp" 'BEGIN{exit !(x < y)}'; then
            min_lp="$l2"
          fi
          if is_spike "$l2"; then
            hits=$(( hits + 1 ))
          else
            clean_gp="$g2"; clean_rt="$t2"; clean_lp="$l2"
            [ -n "$BASE_LOSS" ] || BASE_LOSS="$l2"
          fi
        done
        if [ "$hits" -ge 2 ]; then
          # 首档 0.18% 也可能就是真实 policer 拐点，不能仅凭它低于 0.5%
          # 就收作线路底噪。把最低值留给主流程，必要时向下测控制点再判断.
          SPIKE_MIN_LOSS="$min_lp"
          printf '  %-10s %12s %9s %8s  %s\n' "$r" "$gp" "$rt" "$lp" "$(_c '0;31' "loss spike (${hits}/3)")"
          BROKE_AT=$r; return 0
        fi
        if [ -n "$clean_gp" ]; then
          gp="$clean_gp"; rt="$clean_rt"; lp="$clean_lp"
          [ "$verdict" = ok ] && verdict="transient (1/3), clean recheck used"
        else
          printf '  %-10s %12s %9s %8s  %s\n' "$r" "$gp" "$rt" "$lp" "$(_c '0;33' "transient (1/3), no clean recheck")"
          sleep "$GAP"; continue
        fi
      fi
      # 吞吐远低于限速值、重传却很低 = 整形器压根没被触发, 瓶颈在对端.
      # 「重传低」这个条件必不可少：吞吐低但重传高是真撞限速器, 那是有效数据.
      if awk -v g="$gp" -v r="$r" -v l="$lp" -v t="$thresh" \
         'BEGIN{exit !(g < r*0.7 && l <= t)}' 2>/dev/null; then
        SLOW_HITS=$(( SLOW_HITS + 1 ))
        printf '  %-10s %12s %9s %8s  %s\n' "$r" "$gp" "$rt" "$lp" \
          "$(_c '0;33' "only $(awk -v g="$gp" -v r="$r" 'BEGIN{printf "%d", g*100/r}')% of target")"
        [ "$SLOW_HITS" -ge 3 ] && { PEER_TOO_SLOW=1; SLOW_AT="$r"; return 0; }
        LAST_OK=$r; prev_gp=$gp; sleep "$GAP"; continue
      fi
      SLOW_HITS=0
      # 吞吐不再增长也说明到顶了
      if [ "$verdict" = ok ] && awk -v x="$gp" -v y="$prev_gp" 'BEGIN{exit !(y>0 && x < y*1.01)}'; then verdict="no further gain"; fi
      printf '  %-10s %12s %9s %8s  %s\n' "$r" "$gp" "$rt" "$lp" "$verdict"
      LAST_OK=$r; prev_gp=$gp
      sleep "$GAP"
    done
  }

  # ── 不限速探测 ──────────────────────────────────────────────────────────
  # 直接放开跑一次: 丢包低 = 没东西在打你 = 不用整形; 丢包高 = 有限速器, 再去找它.
  #
  # 关键: 拐点在【不限速吞吐之上】, 不是之下. 打穿限速器会让吞吐掉下来 ——
  # LA 机不限速 481 Mbps / 丢包 5.70%, 而真实拐点在 530(限到 530 反而跑 499);
  # 美国机不限速 1262 / 3.44%, 拐点 1340. 从不限速吞吐往下找会直接错过.
  #
  # 使用单连接观察限速器，避免多连接并发掩盖单连接表现。
  local ug="" ug_recv="" ulp="" cap_gp="" cap_streams=1 single_printed=0
  if [ -z "$user_range" ]; then
    info "Unshaped probe (no rate limit, ${dur}s, 1 stream)"
    printf '  %-10s %12s %9s %8s  %s\n' "Rate/Mbit" "Goodput/Mbps" "Retrans" "Loss%" "Verdict"
    qdisc_set_fq "$iface" || { qdisc_restore; warn "failed to enable fq for unshaped probe"; return 2; }
    local ures urt
    for _ in 1 2 3; do ures=$(run_iperf "$peer" "$dur" 1); [ -n "$ures" ] && break; sleep 8; done
    [ -n "$ures" ] || { qdisc_restore; warn "unshaped probe failed, check the peer"; return 2; }
    ug=$(echo "$ures" | awk '{print $1}'); urt=$(echo "$ures" | awk '{print $2}')
    ug_recv=$(echo "$ures" | awk '{print $3}')
    ulp=$(loss_pct "$urt" "$ug" "$dur")
    cap_gp="${ug_recv:-$ug}"

    # 自动带宽探测用 4 流, 这里用单流找 policer. 公共节点偶发拥塞时, 单次
    # receiver 可能只剩自动探测值的一小部分, 直接拿它推扫描区间会把 40M
    # 机器误扫成 14M、最终持久限到 12M. 只在低于 70% 时补两次, 正常机器
    # 不增加时长；三次取 receiver 最高的【整组】结果, sender/重传必须同步换.
    # 大带宽机（>=AGG_MIN）沿用后面的 8 流保护, 不在这里重复增加两轮单流.
    if [ -n "$nominal" ] && [ "$nominal" -lt "$AGG_MIN" ] 2>/dev/null && awk -v g="$cap_gp" -v n="$nominal" \
       'BEGIN{exit !(g < n*0.7)}'; then
      local best_res="$ures" best_gp="$cap_gp" samples=1 sample_n extra
      local es er egp ert elp
      info "Single stream reached ${cap_gp} Mbps (<70% of ${nominal}); taking 2 more samples"
      printf '  %-10s %12s %9s %8s  %s\n' "none (#1)" "$cap_gp" "$urt" "$ulp" "sample"
      for sample_n in 2 3; do
        sleep "$GAP"
        extra=""
        for _ in 1 2 3; do extra=$(run_iperf "$peer" "$dur" 1); [ -n "$extra" ] && break; sleep 8; done
        if [ -z "$extra" ]; then
          printf '  %-10s %12s %9s %8s  %s\n' "none (#${sample_n})" "-" "-" "-" "peer busy, skipped"
          continue
        fi
        samples=$(( samples + 1 ))
        es=$(echo "$extra" | awk '{print $1}'); ert=$(echo "$extra" | awk '{print $2}')
        er=$(echo "$extra" | awk '{print $3}'); egp="${er:-$es}"
        elp=$(loss_pct "$ert" "$es" "$dur")
        printf '  %-10s %12s %9s %8s  %s\n' "none (#${sample_n})" "$egp" "$ert" "$elp" "sample"
        if awk -v x="$egp" -v y="$best_gp" 'BEGIN{exit !(x > y)}'; then
          best_res="$extra"; best_gp="$egp"
        fi
      done
      ures="$best_res"
      ug=$(echo "$ures" | awk '{print $1}'); urt=$(echo "$ures" | awk '{print $2}')
      ug_recv=$(echo "$ures" | awk '{print $3}')
      ulp=$(loss_pct "$urt" "$ug" "$dur")
      cap_gp="${ug_recv:-$ug}"
      info "Using best of ${samples}: ${cap_gp} Mbps"
    fi
    qdisc_restore

    # 大带宽机不能只凭单流决定是否进入扫描. 长 RTT / 对端接收窗口会把 10G
    # 机器的单流压到 2.5G 以下; 如果这条单流又恰好有路径丢包, 旧逻辑会把
    # 它误认成低速 policer 并扫描几百兆区间. 只在用户标称值已达到 AGG_MIN、
    # 且单流结果确实可疑时补一次 8 流确认，不给普通低带宽扫描增加流量.
    if [ -n "$nominal" ] && [ "$nominal" -ge "$AGG_MIN" ] 2>/dev/null && \
       awk -v g="$cap_gp" -v c="$cap" -v l="$ulp" -v t="$thresh" \
         'BEGIN{exit !(g <= c && l > t)}'; then
      local ares="" ag art ar alp
      info "Single stream is inconclusive on a ${nominal} Mbit host; checking 8-stream aggregate"
      qdisc_set_fq "$iface" || { qdisc_restore; warn "failed to enable fq for aggregate probe"; return 2; }
      for _ in 1 2 3; do ares=$(run_iperf "$peer" "$dur" 8); [ -n "$ares" ] && break; sleep 8; done
      qdisc_restore
      if [ -n "$ares" ]; then
        ag=$(echo "$ares" | awk '{print $1}'); art=$(echo "$ares" | awk '{print $2}')
        ar=$(echo "$ares" | awk '{print $3}'); [ -n "$ar" ] || ar="$ag"
        alp=$(loss_pct "$art" "$ag" "$dur")
        if awk -v g="$ar" -v c="$cap" 'BEGIN{exit !(g > c)}'; then
          printf '  %-10s %12s %9s %8s  %s\n' "none (1x)" "$cap_gp" "$urt" "$ulp" "inconclusive"
          printf '  %-10s %12s %9s %8s  %s\n' "none (8x)" "$ar" "$art" "$alp" "above cap"
          cap_gp="$ar"; cap_streams=8
        else
          printf '  %-10s %12s %9s %8s  %s\n' "none (1x)" "$cap_gp" "$urt" "$ulp" "$(_c '0;31' 'loss -- possible policer')"
          printf '  %-10s %12s %9s %8s  %s\n' "none (8x)" "$ar" "$art" "$alp" "aggregate below cap"
        fi
        single_printed=1
      fi
    fi

    if awk -v g="$cap_gp" -v c="$cap" 'BEGIN{exit !(g > c)}'; then
      [ "$single_printed" = 1 ] || \
        printf '  %-10s %12s %9s %8s  %s\n' "none" "$cap_gp" "$urt" "$ulp" "above cap"
      echo
      warn "不限速 ${cap_streams} 流能送达 ${cap_gp} Mbps, 超过 ${cap} Mbit 的扫描上限."
      echo "  这个量级基本只有专线和内网, 限速器很少见."
      echo "  确定要扫的话: tcpfit sweep --peer <对端> --cap <更大的值>"
      mkdir -p "$STATE_DIR"; printf 'NO_KNEE=1\nABOVE_CAP=%s\nUNSHAPED=%s\n' "$cap" "$cap_gp" > "$STATE_DIR/sweep.result"
      traffic_report
      return 3
    fi

    if ! awk -v l="$ulp" -v t="$thresh" 'BEGIN{exit !(l > t)}'; then
      printf '  %-10s %12s %9s %8s  %s\n' "none" "$cap_gp" "$urt" "$ulp" "ok"
      echo
      warn "不限速送达 ${cap_gp} Mbps, 丢包 ${ulp}%, 未检测到限速器."
      mkdir -p "$STATE_DIR"; printf 'NO_KNEE=1\nUNSHAPED=%s\n' "$cap_gp" > "$STATE_DIR/sweep.result"
      traffic_report
      return 3
    fi

    [ "$single_printed" = 1 ] || \
      printf '  %-10s %12s %9s %8s  %s\n' "none" "$cap_gp" "$urt" "$ulp" "$(_c '0;31' 'loss -- policer present')"
    # ug 是 iperf3【发送端】的数字, 它包含了"写进 socket 但没送达"的部分 ——
    # 干净链路上和接收端只差 3%, 但丢包链路上差很多. 实测一台香港 CN2:
    # 不限速 发送 18.3 / 接收 14.6（丢包 23.6%）, 而干净区上限只有 15.
    # 直接拿 18.3 推区间会得到 17→31, 起点就已经在丢包区里, 整个扫描跑偏.
    # receiver 是 iperf3 实测送达量, 比按重传率反推更准确. 极老版本没有
    # receiver 汇总时第三列为空, 保留原公式作为兼容兜底.
    local ug_eff="$ug_recv"
    [ -n "$ug_eff" ] || ug_eff=$(awk -v g="$ug" -v l="$ulp" 'BEGIN{
      v=g*(1-l/100); if(v<1)v=1; printf "%.1f", v }')
    # 拐点在（真实送达量）之上, 所以区间从它稍下方起, 往上扫.
    # 打穿限速器后 goodput 会掉下来, 丢得越狠掉得越多, 所以上界要按丢包率放宽:
    # 24% 丢包时真实拐点可能比 1.25×goodput 高得多(群里碰到过 177Mbps/24% 的例子).
    lo=$(awk -v g="$ug_eff" 'BEGIN{v=int(g*0.95); if(v<1)v=1; printf "%d", v}')
    hi=$(awk -v g="$ug_eff" -v c="$cap" -v l="$ulp" 'BEGIN{
      k = 1.25 + l/100*2          # 丢包越高, 真实拐点离 goodput 越远
      if (k > 2.5) k = 2.5
      v = g*k; if (v > c) v = c
      printf "%d", v }')
    [ "$hi" -gt "$lo" ] 2>/dev/null || hi=$(( lo + 2 ))
    [ -n "$nominal" ] || nominal=$(awk -v g="$ug_eff" 'BEGIN{printf "%d", g}')
    # 步长必须按【区间宽度】推, 不能按 nominal 推 —— calc_step 对 ≤600M 恒等于 20,
    # 而小带宽机器的区间可能只有十几宽, 20 的步长只能采到 1-2 个点, 定不出拐点.
    # 实测香港 CN2: 区间 17→31（宽 14）, step 20 → 只测了 17 和 31 两档.
    # 目标是区间内约 10 个采样点; 大机器上这个公式给出的值和 calc_step 基本一致.
    [ -n "$step" ] || step=$(awk -v lo="$lo" -v hi="$hi" 'BEGIN{
      s = int((hi-lo)/10 + 0.5); if (s < 1) s = 1; printf "%d", s }')
    info "Policer present, scanning ${lo} -> ${hi} Mbit（不限速实测送达 ${ug_eff} Mbps）"
    info "Cooling down ${PRE_SCAN_GAP}s before the first scan point"
    sleep "$PRE_SCAN_GAP"
  fi

  echo
  info "Scanning ${lo} -> ${hi} Mbit, step ${step}, ${dur}s each, threshold loss > ${thresh}%"
  printf '  %-10s %12s %9s %8s  %s\n' "Rate/Mbit" "Goodput/Mbps" "Retrans" "Loss%" "Verdict"
  scan_range "$lo" "$hi" "$step"

  # 自动区间第一档就连续丢包时，起点可能已越过一个很浅的 policer 拐点；
  # 也可能只是远端路径稳定的 0.1%-0.3% 底噪。向下 25% 测控制点来区分：
  #   低速档干净        -> 首档是真拐点，保留这个上下界供细扫；
  #   两档损失相近且<=0.5% -> 才确认是线路底噪，再从原区间继续扫.
  # 最多向下三次，宁可不给整形值，也不把仍在丢包区的值推荐给用户.
  if [ -z "$user_range" ] && [ -z "$LAST_OK" ] && [ -n "$BROKE_AT" ]; then
    local known_broke="$BROKE_AT" known_loss="$SPIKE_MIN_LOSS"
    local control control_loss attempts=0
    while [ "$attempts" -lt 3 ] && [ -z "$LAST_OK" ]; do
      attempts=$(( attempts + 1 ))
      control=$(( known_broke * 3 / 4 ))
      [ "$control" -lt 1 ] && control=1
      [ "$control" -lt "$known_broke" ] || break
      info "First scan point is lossy; checking ${control} Mbit as a lower-rate control"
      LAST_OK=""; BROKE_AT=""; BASE_LOSS=""; SPIKE_MIN_LOSS=""
      scan_range "$control" "$control" 1

      if [ -n "$LAST_OK" ]; then
        BROKE_AT="$known_broke"
        break
      fi
      control_loss="$SPIKE_MIN_LOSS"
      if [ -n "$control_loss" ] && [ -n "$known_loss" ] && \
         awk -v a="$control_loss" -v b="$known_loss" -v c="$BASELINE_CAP" 'BEGIN{
           d=a-b; if(d<0)d=-d
           exit !(a<=c && b<=c && d<=0.1)
         }'; then
        BASE_LOSS=$(awk -v a="$control_loss" -v b="$known_loss" 'BEGIN{print (a<b?a:b)}')
        LAST_OK="$control"
        BROKE_AT=""
        info "Stable path loss confirmed at ${BASE_LOSS}%; continuing with a lower-rate baseline"
        scan_range "$known_broke" "$hi" "$step"
        break
      fi
      [ -n "$control_loss" ] || break
      known_broke="$control"
      known_loss="$control_loss"
    done
  fi

  if [ "$PEER_TOO_SLOW" = 1 ]; then
    echo
    trap - INT TERM HUP
    restore_qdisc
    [ "$WIZARD" = 1 ] && printf '\n  %s════ 结果 ══════════════════════════════════════════════%s\n' "$bold" "$plain"
    echo
    # 连续三档达不到限速值, 原因有两种, 早期版本一律报"对端太慢".
    # 扫描上限还是 2500 时这个错标很少露面; 抬到 10000 之后大机器会经常走到这里,
    # 被指使去换对端, 换完还是一样 —— 因为瓶颈根本在本机.
    # 判据: 掉速发生在【不限速实测吞吐】之上, 说明这速率本机本来就跑不到.
    # 实测依据: 1 核机器在 3000 Mbit 档 HTB 只能送达 71.5%, 而判据线是 70%.
    if [ -n "$cap_gp" ] && [ -n "$SLOW_AT" ] && \
       awk -v s="$SLOW_AT" -v g="$cap_gp" 'BEGIN{exit !(g > 0 && s > g*1.1)}' 2>/dev/null; then
      warn "扫到 ${SLOW_AT} Mbit 时连续达不到目标 —— 这台机器跑不到这个速率."
      echo
      echo "  不限速实测 ${cap_gp} Mbps, 而扫描已经走到 ${SLOW_AT} Mbit."
      echo "  可能的原因："
      echo "    1) 这条线没有限速器, 到 ${cap_gp} 就到顶了 —— 那本来就不需要整形"
      echo "    2) CPU 推不动这个速率的 HTB（核少的机器 3Gbit 以上会明显掉速）"
      echo "    3) 对端不够快, 换公共节点（选对端时回车）再试一次"
    else
      warn "对端速率不够, 无法测出本机限速器 —— 已暂停调优."
      echo
      echo "  怎么办："
      echo "    1) 换一个更快的对端. 对端带宽必须明显高于本机（${nominal}Mbps）"
      echo "    2) 直接用公共节点（选对端时回车）, Leaseweb 机房带宽足够"
      echo "    3) 如果确定本机带宽没那么高, 重跑时把带宽填成实际值"
    fi
    echo
    info "基础调优（拥塞控制 / 缓冲区）已生效."
    traffic_report
    return 2
  fi

  # 粗扫只能定位到「拐点在 LAST_OK 与 BROKE_AT 之间」, 区间宽度就是步长.
  # 在这个区间用 1/4 步长再扫一遍, 把真实上限找准 —— 步长 20 时能多挖回十几 Mbps.
  # 只要粗扫区间里还存在未测过的整数档位就细扫. 小带宽机器常见
  # 26M 干净、28M 丢包；旧的 >5 条件会漏掉 27M, 白白多退 1M.
  if [ "$refine" = 1 ] && [ -n "$LAST_OK" ] && [ -n "$BROKE_AT" ] && [ $(( BROKE_AT - LAST_OK )) -gt 1 ]; then
    local fine coarse_broke
    coarse_broke=$BROKE_AT                       # 先存下粗扫的上界, 下面会被 scan_range 重置
    # 下限 1 而不是 5 —— 步长本身现在按区间宽度推, 小机器上可能只有 1-2,
    # 硬性抬到 5 会让细扫比粗扫还粗.
    fine=$(( step / 4 )); [ "$fine" -lt 1 ] && fine=1
    echo
    info "Knee between ${LAST_OK} and ${coarse_broke}, refining with step ${fine}"
    printf '  %-10s %12s %9s %8s  %s\n' "Rate/Mbit" "Goodput/Mbps" "Retrans" "Loss%" "Verdict"
    BROKE_AT=""
    scan_range $(( LAST_OK + fine )) $(( coarse_broke - fine )) "$fine"
    # 细扫在更细的档位上可能都不触发阈值(拐点就在 coarse_broke 那一档).
    # 不恢复的话 BROKE_AT 是空的, 后面会误判成"未检测到限速器"而不整形 ——
    # 群里实测碰到: 粗扫 536 已经 0.47%-0.63% 丢包, 细扫 521/526/531 都干净,
    # 结果报"未检测到限速器". 粗扫的结论必须保留.
    [ -n "$BROKE_AT" ] || BROKE_AT="$coarse_broke"
  fi

  echo
  trap - INT TERM HUP
  restore_qdisc
  echo
  local knee="$LAST_OK"
  [ -n "$knee" ] || { warn "no usable rate measured, check that the peer is reachable"; return 2; }

  # 扫到区间上界都没有丢包跳变 —— 说明扫描范围内不存在限速器.
  # 早期版本把区间上界当成拐点, 于是给一台根本没有 policer 的机器套了个上限
  # (用户一台 500M 标称的机器被设成 585, 而它实际能跑 9.3 Gbps).
  if [ -z "$BROKE_AT" ]; then
    echo
    if [ -n "$ug" ] && awk -v l="${ulp:-0}" -v t="$thresh" 'BEGIN{exit !(l > t)}'; then
      # 不限速时明明高丢包, 说明限速器确实存在, 只是不在扫描范围内 ——
      # 这跟"没有限速器"是两回事, 不能混为一谈.
      warn "不限速时丢包 ${ulp}%, 但扫到上界 ${hi} Mbit 仍未定位到拐点."
      echo "  限速器应该存在, 只是不在本次扫描范围内. 可以扩大范围重扫:"
      echo "    $(disp) sweep --peer <对端> --from ${hi} --to $(( hi * 2 ))"
      mkdir -p "$STATE_DIR"; printf 'OUT_OF_RANGE=1\nSCANNED_TO=%s\n' "$hi" > "$STATE_DIR/sweep.result"
      traffic_report
      return 3
    fi
    warn "扫到 ${hi} Mbit 仍未出现丢包跳变, 未检测到限速器."
    mkdir -p "$STATE_DIR"; printf 'NO_KNEE=1\nSCANNED_TO=%s\n' "$hi" > "$STATE_DIR/sweep.result"
    traffic_report
    return 3
  fi
  # 安全余量按标称带宽分档. 早期用固定 20Mbit, 在 300M 机器上白丢 19Mbps
  # （实测 300 档重传比 280 档还少）, 说明一个数字套所有带宽不合理.
  [ -n "$margin" ] || margin=$(calc_margin "$nominal")
  local final=$(( knee - margin )); [ "$final" -lt 1 ] && final=$knee
  mkdir -p "$STATE_DIR"; echo "KNEE=$knee"$'\n'"RECOMMEND=$final" > "$STATE_DIR/sweep.result"
  # 国际线路调优流程里这些数字由 wizard 在「结果」里统一呈现, 这里只出执行日志
  if [ "$WIZARD" = 1 ]; then
    ok "Knee ${knee} Mbit, margin ${margin} Mbit -> shape at ${final} Mbit"
    return 0
  fi
  ok "实测上限 ${knee} Mbit, 按 ${nominal}M 档位退 ${margin} 余量 → 建议整形值 ${final} Mbit"
  echo
  echo "  应用: $(disp) shape --rate $final"
  echo "  (扫描本身不会改变整形配置, 上面这条才会)"
  traffic_report
}

# ── 验证与状态 ──────────────────────────────────────────────────────────────
cmd_status(){
  local iface; iface=$(detect_iface)
  echo "── Current configuration ──"
  kv "Kernel"      "$(uname -r)"
  kv "Congestion"  "$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null)"
  kv "Default qdisc" "$(sysctl -n net.core.default_qdisc 2>/dev/null)"
  kv "Active qdisc" "$(tc qdisc show dev "$iface" 2>/dev/null | head -1 | awk '{print $2}')"
  kv "Egress shaper" "$(first_or "$(r=$(tc_rate_mbit "$(tc class show dev "$iface" 2>/dev/null)") && echo "${r} Mbit")" none)"
  kv "tcp_rmem"    "$(sysctl -n net.ipv4.tcp_rmem 2>/dev/null | tr '\t' ' ')"
  kv "tcp_wmem"    "$(sysctl -n net.ipv4.tcp_wmem 2>/dev/null | tr '\t' ' ')"
  kv "tcp_mem"     "$(sysctl -n net.ipv4.tcp_mem 2>/dev/null | awk '{printf "%.0fM/%.0fM/%.0fM", $1*4/1024,$2*4/1024,$3*4/1024}')"
  kv "Backlog"     "$(sysctl -n net.core.netdev_max_backlog 2>/dev/null)"
  kv "initcwnd"    "$(ip route show default | grep -oE 'initcwnd [0-9]+' || echo '默认(10)')"
  kv "tcpfit conf" "$([ -f "$SYSCTL_FILE" ] && echo applied || echo absent)"
  kv "Shaper svc"  "$(systemctl is-enabled tcpfit-qdisc.service 2>/dev/null || echo 未安装)"
  kv "Snapshot"    "$([ -f "$SNAPSHOT" ] && echo "$SNAPSHOT" || echo 无)"
  echo
  echo "── Health ──"
  local out rt
  out=$(awk '/^Tcp: [0-9]/{print $12, $13}' /proc/net/snmp)
  rt=$(echo "$out" | awk '{if($1>0) printf "%.3f%%", $2*100/$1; else print "n/a"}')
  kv "Retrans (boot)" "$rt  (cumulative since boot; use Verify for current)"
  kv "qdisc drops" "$(first_or "$(grep -oP 'dropped \K[0-9]+' <<<"$(tc -s class show dev "$iface" 2>/dev/null)")" n/a)"
  kv "Memory"      "$(free -m | awk '/Mem:/{print "已用 "$3"MB / 可用 "$7"MB / 共 "$2"MB"}')"
  kv "Swap"        "$(free -m | awk '/Swap:/{if($2==0) print "none (recommended on low-memory hosts)"; else print $3"/"$2" MB"}')"
  # grep -c 无匹配时输出 0 但退出码 1, 不能用 || 兜底, 否则会打印两个 0
  kv "OOM (1h)"    "$(journalctl --since '-1 hour' 2>/dev/null | grep -c 'oom-kill') in last hour"
}

# 验证「本机端口能力」. 刻意用近端对端 —— 测的是服务器出口能发多快、
# 整形有没有生效, 不是到国内的速度（那取决于线路质量, 见 cmd_cntest）.
# 实测 + 判定拆开：国际线路调优流程要把执行日志（英文）和结论（中文）分在两段里打印.
VS1=""; VG1=""; VR1=""; VS4=""; VG4=""; VR4=""; VDUR=10
verify_measure(){
  local peer="$1" res
  VS1=""; VG1=""; VR1=""; VS4=""; VG4=""; VR4=""
  res=$(run_iperf "$peer" "$VDUR" 1); [ -n "$res" ] && {
    VS1=$(echo "$res"|awk '{print $1}'); VR1=$(echo "$res"|awk '{print $2}')
    VG1=$(echo "$res"|awk '{print $3}'); [ -n "$VG1" ] || VG1="$VS1"
  }
  sleep 3
  res=$(run_iperf "$peer" "$VDUR" 4); [ -n "$res" ] && {
    VS4=$(echo "$res"|awk '{print $1}'); VR4=$(echo "$res"|awk '{print $2}')
    VG4=$(echo "$res"|awk '{print $3}'); [ -n "$VG4" ] || VG4="$VS4"
  }
  [ -n "$VG1" ] && [ -n "$VR1" ] && [ -n "$VG4" ] && [ -n "$VR4" ]
}

# 打印验证结果表 + 结论. $1 = 当前整形值(Mbit, 可空)
#
# 判定一律用丢包率, 不用绝对重传次数 —— 同样 14574 次, 300M 机上是 5.6% 的灾难,
# 9G 机上只有 0.19% 属正常. sweep 早就改成丢包率了, verify 这里当时漏了同步,
# 结果给一台 5Gbps 的机器报"重传偏高, 整形值可能设高了", 而那台根本没装整形.
#
#   < 0.05%    干净
#   0.05-0.5%  略高, 通常不影响
#   0.5-1%     偏高, 值得查
#   > 1%       很糟, 多半撞了限速器或链路有问题
# 参照: 实测干净的机器在 0.0013%-0.0017%, 真撞限速器是 1.35%-6.50%.
verify_verdict(){
  local target="${1:-}" lp1="" lp4=""
  [ -n "$VS1" ] && [ -n "$VR1" ] && lp1=$(loss_pct "$VR1" "$VS1" "$VDUR")
  [ -n "$VS4" ] && [ -n "$VR4" ] && lp4=$(loss_pct "$VR4" "$VS4" "$VDUR")
  echo "  验证"
  printf '      %s %s %s %s\n' "$(_pad "" 14)" "$(_rpad "吞吐 Mbps" 12)" "$(_rpad "重传" 9)" "$(_rpad "估算重传比" 10)"
  printf '      %s %s %s %s\n' "$(_pad "单流" 14)"     "$(_rpad "${VG1:-测试失败}" 12)" "$(_rpad "${VR1:--}" 9)" "$(_rpad "${lp1:+${lp1}%}" 10)"
  printf '      %s %s %s %s\n' "$(_pad "4 流并发" 14)" "$(_rpad "${VG4:-测试失败}" 12)" "$(_rpad "${VR4:--}" 9)" "$(_rpad "${lp4:+${lp4}%}" 10)"
  echo
  # 吞吐和整形值比, 给结论而不是丢一堆数字
  if [ -n "$VG4" ] && [ -n "$target" ] && [ "$target" -gt 0 ] 2>/dev/null; then
    local pct; pct=$(awk -v a="$VG4" -v b="$target" 'BEGIN{printf "%.0f", a*100/b}')
    if   [ "$pct" -ge "$VERIFY_GOOD_PCT" ] 2>/dev/null; then ok "达到整形值的 ${pct}%, 端口能力正常"
    elif [ "$pct" -ge "$VERIFY_ACCEPT_PCT" ] 2>/dev/null; then info "达到整形值的 ${pct}%, 偏低但可接受（对端可能被其他人占用）"
    else warn "只达到整形值的 ${pct}%, 建议换个对端重测"; fi
  fi
  [ -n "$lp4" ] || return 0
  # 没有整形时不能说"整形值设高了" —— 用户会被指去调一个根本不存在的东西
  local advice
  if [ -n "$target" ] && [ "$target" -gt 0 ] 2>/dev/null; then
    advice="整形值可能设高了, 可以重跑菜单 4 重新找拐点"
  else
    advice="这台没有应用整形. 高丢包来自链路本身或未被识别的限速器, 可以跑菜单 4 试着扫一次拐点"
  fi
  case "$(retrans_band "$lp4")" in
    0) ok "估算重传比 ${lp4}%, 较低" ;;
    1) ok "估算重传比 ${lp4}%, 略高" ;;
    2) warn "估算重传比 ${lp4}%, 偏高 —— ${advice}" ;;
    3) warn "估算重传比 ${lp4}%, 很高 —— ${advice}" ;;
  esac
}

cmd_verify(){
  local peer="" peer_name="" peer_rtt=""
  while [ $# -gt 0 ]; do
    case "$1" in --peer) peer="$2"; shift 2 ;; --name) peer_name="$2"; shift 2 ;; *) shift ;; esac
  done
  local iface shaper; iface=$(detect_iface)
  shaper=$(tc_rate_mbit "$(tc class show dev "$iface" 2>/dev/null)")

  echo
  printf '  %s本机端口能力验证%s\n' "$bold" "$plain"
  rule
  echo "  测的是：服务器出口能发多快、整形有没有生效"
  echo "  不测：到国内的速度（那取决于线路质量, 跟服务器配置无关）"
  echo

  if [ -z "$peer" ]; then
    warn "没有可用对端, 只显示配置"
    cmd_status; return 0
  fi
  peer_rtt=$(ping $IP_FAMILY -c 2 -q -W 2 "$peer" 2>/dev/null | awk -F'/' '/rtt|round-trip/{printf "%.0f", $5}')
  printf "  对端    %s   RTT %sms   端口 %s\n" "$peer" "${peer_rtt:-?}" "$PEER_PORT"
  printf "  整形    %s\n" "$(first_or "${shaper:+${shaper} Mbit}" 未设置)"
  echo
  command -v iperf3 >/dev/null || { warn "无 iperf3, 跳过实测"; return 0; }

  take_lock
  verify_measure "$peer"
  verify_verdict "$shaper"
  rule
}

# ── 检查更新 ────────────────────────────────────────────────────────────────
cmd_update(){
  need_root
  take_lock
  command -v curl >/dev/null || die "需要 curl"
  # 菜单调进来时带 --from-menu: 更新完要用新版本 exec 掉自己, 否则用户在同一个
  # 菜单里接着操作, 跑的仍是内存里的旧代码.
  local from_menu=0
  [ "${1:-}" = "--from-menu" ] && { from_menu=1; shift; }
  info "检查更新…"
  local latest
  # 只看 release, 不看 main —— main 可能领先于任何已发布版本
  latest=$(curl -fsSL --max-time 10 "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
           | grep -m1 '"tag_name"' | sed 's/.*"tag_name"[[:space:]]*:[[:space:]]*"v\{0,1\}\([^"]*\)".*/\1/')
  [[ "$latest" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "查不到有效的最新版本, 检查网络或稍后再试" 2

  if [ "$latest" = "$VERSION" ]; then ok "已是最新版本 v$VERSION"; return 0; fi
  # 用 sort -V 比版本号, 字符串比较会把 0.3.10 判成小于 0.3.9
  if [ "$(printf '%s\n%s\n' "$VERSION" "$latest" | sort -V | tail -1)" = "$VERSION" ]; then
    ok "当前 v$VERSION 比已发布的 v$latest 还新（开发版）"; return 0
  fi

  echo
  _conf "当前版本" "v$VERSION"
  _conf "最新版本" "v$latest"
  _conf "更新说明" "https://github.com/$REPO/releases/tag/v$latest"
  echo
  confirm "  现在更新？" y || { info "已取消"; return 0; }

  # 主程序和优化线路调优模块必须来自同一版本，并逐个通过发布清单校验。
  command -v sha256sum >/dev/null || die "需要 sha256sum 校验更新文件"
  local dl; dl=$(mktemp -d)
  local base="https://github.com/$REPO/releases/download/v$latest"
  if ! curl -fsSL --max-time 60 "$base/tcpfit.sh" -o "$dl/tcpfit.sh"; then
    rm -rf "$dl"; die "下载失败" 2
  fi
  if curl -fsSL --max-time 20 "$base/SHA256SUMS" -o "$dl/SHA256SUMS"; then
    if ! ( cd "$dl" && grep ' tcpfit\.sh$' SHA256SUMS | sha256sum -c - >/dev/null 2>&1 ); then
      rm -rf "$dl"; die "SHA256 校验不通过, 未更新" 2
    fi
    info "SHA256 校验通过"
  else
    rm -rf "$dl"; die "取不到 SHA256SUMS，未更新" 2
  fi
  if ! { starts_with "$(head -1 "$dl/tcpfit.sh" 2>/dev/null)" '#!' && grep -q "^VERSION=\"$latest\"" "$dl/tcpfit.sh"; }; then
    rm -rf "$dl"; die "下载的文件校验不通过, 未更新" 2
  fi
  local f lib_dir="/usr/local/lib/tcpfit/$latest"
  for f in tcpfit-return.py tcpfit-client.sh tcpfit-client.ps1; do
    if ! curl -fsSL --max-time 60 "$base/$f" -o "$dl/$f" || \
       ! (cd "$dl" && grep -F "  $f" SHA256SUMS | sha256sum -c - >/dev/null 2>&1); then
      rm -rf "$dl"; die "优化线路调优模块 $f 下载或校验失败，未更新" 2
    fi
  done
  mkdir -p "$lib_dir" || { rm -rf "$dl"; die "无法创建模块目录"; }
  for f in tcpfit.sh tcpfit-return.py tcpfit-client.sh tcpfit-client.ps1; do
    install -m 755 "$dl/$f" "$lib_dir/$f.new" && mv -f "$lib_dir/$f.new" "$lib_dir/$f" || {
      rm -rf "$dl"; die "模块安装失败，主程序未更新";
    }
  done
  # 不能原地覆盖 —— 正在执行的就是 $SELF_PATH, 而 bash 是按文件偏移增量读脚本的,
  # 原地改写有可能让它读到新文件的错位内容（两个版本长度还不一样）.
  # 先写同目录的 .new 再 mv: rename 是原子的, 换新 inode, 旧 inode 对当前进程保持有效.
  if ! install -m 755 "$dl/tcpfit.sh" "${SELF_PATH}.new" || ! mv -f "${SELF_PATH}.new" "$SELF_PATH"; then
    rm -f "${SELF_PATH}.new"; rm -rf "$dl"; die "写入 $SELF_PATH 失败" 2
  fi
  rm -rf "$dl"
  ok "已更新到 v$latest"
  info "配置和快照不受影响."

  # 关键: 磁盘上换了, 但当前进程内存里跑的还是旧代码.
  # 早期版本这里只打一句"重跑一次调优", 用户就在同一个菜单里按 1 —— 跑的仍是旧版本,
  # 于是"更新了但 bug 还在". 有客户真踩过, 排查了很久才定位到是这里.
  if [ "$from_menu" = 1 ]; then
    info "以新版本重启…"
    echo
    exec "$SELF_PATH" menu
  fi
  warn "当前进程跑的仍是 v${VERSION} 的代码, 重新运行 tcpfit 才会用上新版本."
}

# ── 优化线路调优 ────────────────────────────────────────────────────────────────
# 优化线路调优由协调模块管理接入、测量、分阶段调整和恢复，复用本文件的参数应用与测速函数。
return_assets(){
  local here dest dl f
  here=$(cd "$(dirname "$SOURCE_FILE")" 2>/dev/null && pwd)
  if [ -f "$here/tcpfit.sh" ] && [ -f "$here/tcpfit-return.py" ] && [ -f "$here/tcpfit-client.sh" ] && [ -f "$here/tcpfit-client.ps1" ]; then
    RETURN_MAIN="$here/tcpfit.sh"; RETURN_HELPER="$here/tcpfit-return.py"; RETURN_CLIENT="$here/tcpfit-client.sh"
    return 0
  fi
  dest="/usr/local/lib/tcpfit/$VERSION"
  if [ ! -f "$dest/tcpfit-return.py" ] || [ ! -f "$dest/tcpfit-client.sh" ] || [ ! -f "$dest/tcpfit-client.ps1" ] || [ ! -f "$dest/tcpfit.sh" ]; then
    mkdir -p /usr/local/lib/tcpfit || return 1
    dl=$(mktemp -d /usr/local/lib/tcpfit/.download.XXXXXX) || return 1
    local base="https://raw.githubusercontent.com/$REPO/v$VERSION"
    curl -fsSL --max-time 30 "$base/SHA256SUMS" -o "$dl/SHA256SUMS" || { rm -rf "$dl"; return 1; }
    for f in tcpfit.sh tcpfit-return.py tcpfit-client.sh tcpfit-client.ps1; do
      curl -fsSL --max-time 60 "$base/$f" -o "$dl/$f" || { rm -rf "$dl"; return 1; }
      (cd "$dl" && grep -F "  $f" SHA256SUMS | sha256sum -c - >/dev/null 2>&1) || { rm -rf "$dl"; return 1; }
    done
    mkdir -p "$dest" || { rm -rf "$dl"; return 1; }
    for f in tcpfit.sh tcpfit-return.py tcpfit-client.sh tcpfit-client.ps1; do
      install -m 755 "$dl/$f" "$dest/$f" || { rm -rf "$dl"; return 1; }
    done
    rm -rf "$dl"
  fi
  RETURN_MAIN="$dest/tcpfit.sh"; RETURN_HELPER="$dest/tcpfit-return.py"; RETURN_CLIENT="$dest/tcpfit-client.sh"
}

return_dependencies(){
  local c pkg missing=()
  for c in python3 iperf3 curl; do
    command -v "$c" >/dev/null || missing+=("$c")
  done
  [ "${#missing[@]}" = 0 ] && return 0
  info "安装优化线路调优依赖: ${missing[*]}"
  if command -v apt-get >/dev/null; then
    command -v debconf-set-selections >/dev/null && printf 'iperf3 iperf3/start_daemon boolean false\n' | debconf-set-selections
    apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
  elif command -v dnf >/dev/null; then dnf install -y "${missing[@]}"
  elif command -v yum >/dev/null; then yum install -y "${missing[@]}"
  elif command -v apk >/dev/null; then apk add "${missing[@]}"
  elif command -v pacman >/dev/null; then
    for pkg in "${missing[@]}"; do [ "$pkg" != python3 ] || pkg=python; pacman -S --needed --noconfirm "$pkg" || return 1; done
  else warn "找不到支持的包管理器，请安装: ${missing[*]}"; return 1; fi
}

cmd_return_recover(){
  need_root
  take_lock recovery
  [ -f "$STATE_DIR/return-pending.json" ] || { info "没有待恢复的优化线路调优任务"; return 0; }
  return_assets || die "无法加载优化线路调优恢复模块"
  local record
  record=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["record_dir"])' "$STATE_DIR/return-pending.json") || die "恢复记录无法读取"
  python3 "$RETURN_HELPER" recover --record-dir "$record" --lock-fd 9 || return 1
  ok "优化线路调优任务的参数恢复和临时资源清理已完成"
}

# 自动探测只接受对应协议族的公网地址；手动指定仍可使用域名或私网地址。
is_public_server_ip(){
  case "$1" in ''|*[!0-9a-fA-F:.]*) return 1 ;; esac
  awk -v address="$1" -v family="${IP_FAMILY#-}" 'BEGIN {
    if (family == 4) {
      if (address !~ /^[0-9.]+$/ || split(address, octets, ".") != 4) exit 1
      for (i=1; i<=4; i++)
        if (octets[i] !~ /^(0|[1-9][0-9]*)$/ || length(octets[i]) > 3 || octets[i]+0 > 255) exit 1
      a=octets[1]+0; b=octets[2]+0; c=octets[3]+0
      if (a == 0 || a == 10 || a == 127 || a >= 224 ||
          (a == 100 && b >= 64 && b <= 127) ||
          (a == 169 && b == 254) || (a == 172 && b >= 16 && b <= 31) ||
          (a == 192 && (b == 168 || (b == 0 && (c == 0 || c == 2)))) ||
          (a == 198 && (b == 18 || b == 19 || (b == 51 && c == 100))) ||
          (a == 203 && b == 0 && c == 113)) exit 1
    } else {
      address=tolower(address)
      if (address !~ /^[23][0-9a-f][0-9a-f][0-9a-f]:[0-9a-f:]+$/ ||
          address ~ /:::/ || address ~ /[^:]:$/) exit 1
      compressed=address
      if (gsub(/::/, ":", compressed) > 1) exit 1
      n=split(address, groups, ":"); count=0
      for (i=1; i<=n; i++) {
        if (length(groups[i]) > 4) exit 1
        if (groups[i] != "") count++
      }
      if (index(address, "::") ? count >= 8 : count != 8) exit 1
      if (address ~ /^2001:0?db8:/ || address ~ /^2001:0*2:/ ||
          (groups[1] == "3fff" && (length(groups[2]) < 4 || substr(groups[2], 1, 1) == "0"))) exit 1
    }
    exit 0
  }'
}

detect_return_server(){
  local destination=1.1.1.1 route address endpoint
  [ "$IP_FAMILY" != -6 ] || destination=2606:4700:4700::1111
  # 使用内核实际选路的源地址，不遍历可能含容器、隧道的所有网卡。
  route=$(ip "$IP_FAMILY" route get "$destination" 2>/dev/null) || route=""
  address=$(awk '{for(i=1;i<NF;i++) if($i=="src") {print $(i+1); exit}}' <<<"$route")
  if is_public_server_ip "$address"; then printf '%s\n' "$address"; return 0; fi

  # NAT 或本机地址不可用时查询公网出口；忽略代理，避免取得代理服务器的地址。
  for endpoint in https://api64.ipify.org https://icanhazip.com; do
    if command -v curl >/dev/null 2>&1; then
      address=$(curl "$IP_FAMILY" -fsS --noproxy '*' --connect-timeout 2 --max-time 4 "$endpoint" 2>/dev/null) || continue
    elif command -v wget >/dev/null 2>&1; then
      address=$(wget "$IP_FAMILY" -qO- --no-proxy --timeout=4 --tries=1 "$endpoint" 2>/dev/null) || continue
    else
      return 1
    fi
    address=${address%$'\r'}
    if is_public_server_ip "$address"; then printf '%s\n' "$address"; return 0; fi
  done
  return 1
}

cmd_return(){
  local server="" server_bw="" client_bw="" control_port=12223 iperf_port=12224 role=proxy yes=0 ttl=600
  local family_given=0 args=() choice repeats=2 repeats_given=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --server|--server-bw|--client-bw|--control-port|--iperf-port|--role|--token-ttl|--repeats)
        [ $# -ge 2 ] || die "$1 缺少参数"
        case "$1" in
          --server) server="$2" ;; --server-bw) server_bw="$2" ;; --client-bw) client_bw="$2" ;;
          --control-port) control_port="$2" ;; --iperf-port) iperf_port="$2" ;;
          --role) role="$2" ;; --token-ttl) ttl="$2" ;;
          --repeats) repeats="$2"; repeats_given=1 ;;
        esac
        shift 2 ;;
      -4|-6) IP_FAMILY="$1"; family_given=1; shift ;;
      -y|--yes) yes=1; shift ;;
      -h|--help)
        printf '%s\n' '优化线路调优' '用法: tcpfit return [--server 可达地址] [--server-bw Mbps] [--client-bw Mbps]' \
          '       [--control-port 端口] [--iperf-port 端口] [--role proxy|bulk|mixed] [-4|-6] [--yes]' \
          '       [--token-ttl 600]（30-1800 秒，配对成功即失效）' \
          '       [--repeats 2]（每次评估共测 1-10 次，缓冲区只测单连接）' \
          '服务器地址自动探测，失败后才需手动填写；也可用 --server 指定。' \
          '接入端口默认为 TCP 12223，测速端口默认为 TCP 12224，无需填写。' \
          '填写标称带宽时取已填值中的较小值；两端都留空时自动用四连接探测带宽。' \
          '自动测速后按配置序号选择，回车保存推荐配置；--yes 自动保存推荐。'
        return 0 ;;
      *) die "未知优化线路调优参数: $1" ;;
    esac
  done
  need_root
  [ ! -f /etc/openwrt_release ] || die "OpenWrt / iStoreOS 本次仅支持测速端，请执行调优服务器生成的接入命令"
  take_lock
  if [ "$yes" = 0 ]; then
    echo; step "优化线路调优"
    echo "    家宽接入后自动测速，结束后选择保存配置。"
    echo "    需要 python3、iperf3 和 curl，缺少时自动安装。"
  fi
  if [ -z "$server" ]; then
    if [ "$family_given" = 0 ] && [ "$IP_FAMILY" = -4 ] && ! have_ipv4 && have_ipv6; then
      IP_FAMILY=-6
    fi
    info "自动探测服务器 IPv${IP_FAMILY#-} 地址…"
    if server=$(detect_return_server); then
      ok "服务器地址: $server"
    elif [ "$yes" = 0 ]; then
      warn "未能自动获取服务器 IPv${IP_FAMILY#-} 公网地址，请手动填写"
      server=$(ask "  家宽可访问的服务器地址（IP 或域名）" "")
    fi
  fi
  [ -n "$server" ] || die "无法获取服务器可达地址，请使用 --server 指定，或重新进入菜单填写"
  if [ "$yes" = 0 ]; then
    [ -n "$server_bw" ] || server_bw=$(ask "  服务器标称出口 Mbps（回车留空）" "")
    [ -n "$client_bw" ] || client_bw=$(ask "  家宽标称下载 Mbps（回车留空）" "")
    if [ "$repeats_given" = 0 ]; then
      repeats=$(ask "  每次评估的测速次数（1-10）" 2)
    fi
    choice=$(ask "  用途 1) 代理/加速  2) 大文件  3) 混合" 1)
    case "$choice" in 1) role=proxy ;; 2) role=bulk ;; 3) role=mixed ;; *) die "用途必须为 1、2 或 3" ;; esac
  fi
  [ "$control_port" = 0 ] || is_posint "$control_port" 1024 65535 || die "接入端口必须是 1024-65535 的整数，或用 0 自动选择"
  [ "$iperf_port" = 0 ] || is_posint "$iperf_port" 1024 65535 || die "测速端口必须是 1024-65535 的整数，或用 0 自动选择"
  [ "$control_port" = 0 ] || [ "$control_port" != "$iperf_port" ] || die "接入端口和测速端口不能相同"
  is_posint "$ttl" 30 1800 || die "token 有效期必须是 30-1800 秒"
  is_posint "$repeats" 1 10 || die "测速次数必须是 1-10 的整数"
  [ -z "$server_bw" ] || is_posint "$server_bw" 1 1000000 || die "服务器标称带宽必须是 1-1000000 Mbps 的整数"
  [ -z "$client_bw" ] || is_posint "$client_bw" 1 1000000 || die "家宽标称带宽必须是 1-1000000 Mbps 的整数"
  case "$role" in proxy|bulk|mixed) ;; *) die "用途必须是 proxy / bulk / mixed" ;; esac
  echo
  _conf "服务器地址" "$server（IPv${IP_FAMILY#-}）"
  local control_display="$control_port" iperf_display="$iperf_port"
  [ "$control_port" != 0 ] || control_display="自动选择"
  [ "$iperf_port" != 0 ] || iperf_display="自动选择"
  _conf "接入 / 测速端口" "$control_display / $iperf_display TCP"
  _conf "服务器 / 家宽标称" "${server_bw:-未知} / ${client_bw:-未知} Mbps"
  _conf "测试方式" "单连接，每组 $repeats 次"
  _conf "缓冲区试调" "1.5 × BDP 起步，最高 2.5 × BDP，初值不稳定继续试调，不限轮数和总时长"
  if [ "$yes" = 0 ]; then confirm "  开始并等待家宽接入？" y || { info "已取消"; return 0; }; fi
  return_dependencies || die "依赖准备失败，未开始测速"
  return_assets || die "优化线路调优模块准备失败，请使用完整项目或 install.sh 安装同版本文件"
  args=(run --script "$RETURN_MAIN" --client-script "$RETURN_CLIENT" --server "$server"
    --control-port "$control_port" --iperf-port "$iperf_port" --family "${IP_FAMILY#-}"
    --role "$role" --token-ttl "$ttl" --repeats "$repeats" --state-dir "$STATE_DIR" --lock-fd 9)
  [ "$yes" = 0 ] || args+=(--yes)
  [ -z "$server_bw" ] || args+=(--server-bw "$server_bw")
  [ -z "$client_bw" ] || args+=(--client-bw "$client_bw")
  python3 "$RETURN_HELPER" "${args[@]}" &
  local pid=$! rc=0
  trap 'kill -TERM "$pid" 2>/dev/null; wait "$pid"; exit 130' INT TERM HUP
  wait "$pid" || rc=$?
  trap - INT TERM HUP
  return "$rc"
}

# ── 交互式菜单 ──────────────────────────────────────────────────────────────
#
# 设计原则：用户只需要回答"这机器干什么用的", 其余全部自动.
# 尤其是 iperf3 对端 —— 让用户自己挑服务器是最大的使用门槛, 这里自动 ping 一圈选最近的.

# 公共 iperf3 服务器池. 挑选标准：长期在线、允许匿名测试、地理分布覆盖主要机房区域.
# 公共 iperf3 测速节点池. 格式: 主机|地区|提供商
#
# 这些是第三方免费提供的公共测试服务器, sweep 会向它们发送测试流量.
# 节点来源与实测稳定性（2026-08 在欧洲机器上各测 3 次握手）：
#   Leaseweb   全球机房, 18 节点中 15 个 3/3 —— 最稳, 优先用
#   Clouvider  5 节点中仅 2 个 3/3 —— 时好时坏, 作备选
#   OVH        新加坡节点 3/3
# 注: 早期用 timeout 15 测稳定性, 对 280ms+ 的远节点连握手都不够, 误判成不可用.
# 判定节点好坏不能用固定超时 —— 和 RTT 一刀切是同一类错误.
# 完整公共列表见 https://iperf3serverlist.net
PEER_POOL="
speedtest.hkg12.hk.leaseweb.net|香港|Leaseweb
speedtest.sin1.sg.leaseweb.net|新加坡|Leaseweb
sgp.proof.ovh.net|新加坡|OVH
speedtest.syd12.au.leaseweb.net|悉尼|Leaseweb
speedtest.tyo11.jp.leaseweb.net|东京|Leaseweb
speedtest.fra1.de.leaseweb.net|法兰克福|Leaseweb
speedtest.ams2.nl.leaseweb.net|阿姆斯特丹|Leaseweb
ams.speedtest.clouvider.net|阿姆斯特丹|Clouvider
speedtest.lon12.uk.leaseweb.net|伦敦|Leaseweb
lon.speedtest.clouvider.net|伦敦|Clouvider
speedtest.lax12.us.leaseweb.net|洛杉矶|Leaseweb
speedtest.sfo12.us.leaseweb.net|旧金山|Leaseweb
speedtest.sea11.us.leaseweb.net|西雅图|Leaseweb
speedtest.dal13.us.leaseweb.net|达拉斯|Leaseweb
speedtest.chi11.us.leaseweb.net|芝加哥|Leaseweb
speedtest.nyc1.us.leaseweb.net|纽约|Leaseweb
speedtest.mia11.us.leaseweb.net|迈阿密|Leaseweb
speedtest.mtl2.ca.leaseweb.net|蒙特利尔|Leaseweb
"

# 自动挑选对端：先按 RTT 排序, 再逐个验证 iperf3 真的能用（公共服务器常年占线）
auto_pick_peer(){
  local best="" cand rtt name line
  # 兜底: 命令行直接跑 sweep/verify 的人不走向导, 拿不到那边的安装提示.
  # 没有 ping 时下面每个节点都取不到 RTT, sorted 为空 → 静默 return 1,
  # 调用方报 "公共测速服务器暂时都不可用" —— 服务器是无辜的, 得说真话.
  if command -v ping >/dev/null 2>&1 && ! ping_supports_4; then
    warn "本机的 ping 不认 -4（多半是 GNU inetutils 版）, 无法自动选择对端." >&2
    warn "  换成标准版:  apt install -y iputils-ping  /  dnf install -y iputils" >&2
    warn "  或指定对端:  --peer <iperf3服务器>" >&2
    echo ""; return 1
  fi
  if ! command -v ping >/dev/null 2>&1; then
    warn "本机缺少 ping, 无法自动选择对端." >&2
    warn "  安装:  apt install -y iputils-ping   /   dnf install -y iputils" >&2
    warn "  或指定对端:  --peer <iperf3服务器>" >&2
    echo ""; return 1
  fi
  info "自动选择测速对端（测的是本机端口上限, 越近越准）…" >&2
  # 并行 ping 全部节点. 串行时 17 个节点 × 最长 4 秒 = 最坏 68 秒, 用户干等.
  local sorted="" prov tmpd
  tmpd=$(mktemp -d)
  while IFS='|' read -r cand name prov; do
    [ -z "$cand" ] && continue
    ( r=$(ping $IP_FAMILY -c 2 -q -W 2 "$cand" 2>/dev/null | awk -F'/' '/rtt|round-trip/{printf "%.0f", $5}')
      [ -n "$r" ] && echo "$r $cand $name $prov" > "$tmpd/$cand" ) &
  done <<< "$PEER_POOL"
  wait
  sorted=$(cat "$tmpd"/* 2>/dev/null); rm -rf "$tmpd"
  [ -n "$sorted" ] || { echo ""; return 1; }
  # RTT 分级：sweep 测的是本机端口上的限速器, 对端越近越准.
  #   ≤ideal  最佳, 链路干扰可忽略
  #   ≤accept 可用, 但要提醒用户结果可能偏保守
  #   >accept 拒绝, 宁可失败也不给错误结论
  # 早期只有一个 60ms 硬阈值, 结果香港机器上新加坡 61ms 被卡掉、整个流程失败 —— 太死板.
  local ideal="${NETTUNE_PEER_IDEAL_RTT:-50}"
  local accept="${NETTUNE_PEER_MAX_RTT:-100}"
  local fallback="" fallback_rtt="" limit="$accept" FAR_SKIPPED=0
  while read -r rtt cand name prov; do
    [ -z "$cand" ] && continue
    if [ "$rtt" -gt "$limit" ] 2>/dev/null; then
      printf '  %-34s %-10s %-10s RTT %-6s %s\n' "$cand" "$name" "$prov" "${rtt}ms" "too far, skipped" >&2
      FAR_SKIPPED=1
      continue
    fi
    printf '  %-34s %-10s %-10s RTT %-6s ' "$cand" "$name" "$prov" "${rtt}ms" >&2
    # 先探端口, 把"根本不跑 iperf3/被墙"和"跑着但占线"分开 ——
    # 早期两者都报"占线", 用户完全看不出真实原因.
    # 必须轮换: 只探 5201 的话, 出站封了 5201 的机房上所有节点都会被误判(见 PROBE_PORTS).
    if ! probe_peer_port "$cand"; then
      echo "port closed (tried $PROBE_PORTS)" >&2; continue
    fi
    local pport="$PROBE_PORT_OK"
    [ "$pport" = 5201 ] || printf '%s ' "$(_c '0;33' "5201→$pport")" >&2
    # 没装 iperf3 时无法做占线探测（iperf3 要等确认之后才装）,
    # 降级成"端口通就算可用". 选错了也不致命 —— run_iperf 本身会换端口重试.
    if ! command -v iperf3 >/dev/null 2>&1; then
      printf '%s\n' "$(_c '0;32' "reachable (port $pport)")" >&2
      echo "$cand:$pport"; return 0
    fi
    # 这些公共节点都开十个 iperf3 实例（公共列表里标的就是端口范围）.
    # 早期只试 5201, 等于放着 9 个空闲实例不用去跟全世界抢一个, 动不动就"占线".
    # 从预检探通的那个端口起试 —— 5201 被封时能省掉一次 25 秒的超时等待.
    local gp="" try
    for try in $(port_order "$pport"); do
      if timeout $TIMEOUT_FG 25 iperf3 $IP_FAMILY -c "$cand" -p "$try" -t 3 -P 1 >/dev/null 2>&1; then gp="$try"; break; fi
    done
    if [ -n "$gp" ]; then
      if [ "$rtt" -le "$ideal" ] 2>/dev/null; then
        echo "${green}available${plain} (port $gp)" >&2; best="$cand:$gp"; break
      fi
      echo "available (port $gp, distant — held as fallback)" >&2
      [ -z "$fallback" ] && { fallback="$cand:$gp"; fallback_rtt="$rtt"; }
    else
      echo "all $(echo $PORT_POOL | wc -w) ports busy" >&2
    fi
    sleep 2
  done <<< "$(echo "$sorted" | sort -n)"

  if [ -z "$best" ] && [ -n "$fallback" ]; then
    best="$fallback"
    echo >&2
    warn "最近的可用对端是 ${fallback_rtt}ms（理想是 ${ideal}ms 以内）." >&2
    warn "距离越远, 链路本身的丢包抖动越会混进测量, 拐点可能偏保守." >&2
    warn "结果仍然可用, 只是可能没榨到极限." >&2
  fi

  # 全部节点都超过 accept 时, 早期版本直接失败 —— 国内机器上 18 个节点无一幸免,
  # 整个流程走不下去（issue #4）. 但"远"只是让拐点偏保守, 不是测不了.
  # 所以改成: 说清楚代价, 然后放开距离限制再扫一遍.
  if [ -z "$best" ] && [ -z "$fallback" ] && [ "$FAR_SKIPPED" = 1 ]; then
    echo >&2
    warn "所有公共节点都超过 ${accept}ms —— 本机多半在国内, 或线路绕远." >&2
    warn "距离远会让链路本身的丢包混进测量, 扫出的拐点偏保守（宁可低不冒高）." >&2
    warn "结果仍然可用. 想更准就自己在近处开一台 iperf3 -s, 用 --peer 指定." >&2
    echo >&2
    info "放开距离限制, 用最近的节点重新试…" >&2
    limit=100000
    while read -r rtt cand name prov; do
      [ -z "$cand" ] && continue
      printf '  %-34s %-10s %-10s RTT %-6s ' "$cand" "$name" "$prov" "${rtt}ms" >&2
      if ! probe_peer_port "$cand"; then
        echo "port closed (tried $PROBE_PORTS)" >&2; continue
      fi
      local pport2="$PROBE_PORT_OK"
      if ! command -v iperf3 >/dev/null 2>&1; then
        printf '%s\n' "$(_c '0;32' "reachable (port $pport2)")" >&2
        echo "$cand:$pport2"; return 0
      fi
      local gp2="" try2
      for try2 in $(port_order "$pport2"); do
        if timeout $TIMEOUT_FG 25 iperf3 $IP_FAMILY -c "$cand" -p "$try2" -t 3 -P 1 >/dev/null 2>&1; then gp2="$try2"; break; fi
      done
      if [ -n "$gp2" ]; then
        echo "$(_c '0;33' "available (port $gp2, 距离超标但可用)")" >&2
        best="$cand:$gp2"; break
      fi
      echo "all $(echo $PORT_POOL | wc -w) ports busy" >&2
      sleep 2
    done <<< "$(echo "$sorted" | sort -n | head -5)"
  fi

  if [ -z "$best" ]; then
    warn "公共测速服务器都不可用（要么端口不通, 要么全部占线）." >&2
    warn "公共服务器一次只接一个测试, 等几分钟再试通常就有了." >&2
    warn "或者自己开一台近处的机器跑 iperf3 -s, 然后用 --peer 指定." >&2
    return 1
  fi
  echo "$best"
}

# 验证对端路径是否干净. RTT 只是代理指标 —— 真正要的是路径没有丢包干扰测量.
# 用标称带宽的 40% 跑一次：这个速率远低于任何限速器, 此时还有明显重传,
# 就说明是链路本身在丢包, 拿它测拐点必然测偏.
validate_peer(){
  local peer="$1" nominal="$2" iface="$3"
  # 低带宽线路不能硬抬到 20M: 15M 线路会被验证流量自己打穿.
  local rate=$(( nominal * 40 / 100 )); [ "$rate" -lt 1 ] && rate=1
  qdisc_save "$iface"
  # 早期版本这里没有任何 trap: 中断就把机器留在标称 40% 的限速上, 直到重启
  trap 'qdisc_restore; exit 130' INT TERM HUP
  apply_test_shaper "$iface" "$rate" || { qdisc_restore; echo "unreachable"; return 1; }
  local res rt
  for _ in 1 2; do res=$(run_iperf "$peer" 8 2); [ -n "$res" ] && break; sleep 5; done
  trap - INT TERM HUP
  qdisc_restore
  [ -n "$res" ] || { echo "unreachable"; return 1; }
  local sg gp lp
  sg=$(echo "$res" | awk '{print $1}'); rt=$(echo "$res" | awk '{print $2}')
  gp=$(echo "$res" | awk '{print $3}'); [ -n "$gp" ] || gp="$sg"
  lp=$(loss_pct "$rt" "$sg" 8)
  # 对端连 40% 速率都跑不到, 说明它本身就比本机慢, 拿它测限速器毫无意义.
  # 只有低重传时才能这样判断；高重传造成的低吞吐属于脏路径, 不是慢对端.
  if awk -v g="$gp" -v r="$rate" -v l="$lp" \
     'BEGIN{exit !(g < r*0.7 && l <= 0.05)}' 2>/dev/null; then
    echo "slow:$gp/$rate"; return 1
  fi
  # 低速率下丢包率应该接近 0. 比 sweep 更严(0.05% vs 0.1%), 因为跑的是 40% 速率.
  if awk -v l="$lp" 'BEGIN{exit !(l > 0.05)}' 2>/dev/null; then echo "dirty:${rt}(${lp}%)"; return 1; fi
  echo "clean:$rt"
}

# 曾用它清"超前输入"防止杂散回车误答, 但它会把管道/脚本喂进来的合法输入
# 一起吃掉（实测卡在带宽提示不动）, 手速快的用户也会中招.
# 主操作默认值改成 y 之后, 杂散回车本身已无害, 所以不再调用.
flush_input(){ :; }

# 只给"按任意键返回"用. 调优一跑十几分钟, 其间用户随手按的键留在 tty 缓冲里,
# 提示符一出来就被瞬间吃掉 -> 结果页面没看见就回了菜单.
# 故意不用在 ask/confirm 上, 原因见上面 flush_input 的注释.
# 坑: read -t 0 只判断"有没有数据", 不消费数据, 用它是死循环; 超时必须非零.
# 坑2: 重定向从左往右生效, </dev/tty 要写在 2>/dev/null 后面, 否则无 tty 时报错漏出来.
drain_tty(){ while read -rsn1 -t 0.05 2>/dev/null </dev/tty; do :; done; return 0; }

ask(){  # ask "问题" "默认值"  -> 回显用户输入或默认值
  local q="$1" d="${2:-}" a
  if [ -n "$d" ]; then printf '%s [%s]: ' "$q" "$d" >&2; else printf '%s: ' "$q" >&2; fi
  # 2>/dev/null 必须写在 </dev/tty 前面（重定向从左往右生效）——
  # 否则无 tty 时（管道调用、CI、ssh host tcpfit < /dev/null）
  # 每问一次就往屏幕漏一行 "/dev/tty: No such device or address".
  read -r a 2>/dev/null </dev/tty || a=""
  echo "${a:-$d}"
}

# confirm "问题" [默认]  -> 0=是 1=否. 默认 y 时空回车即同意.
# 主操作（如"开始调优？"）必须默认 y —— 用户就是为这个来的,
# 一个杂散回车不该让整个流程静默取消.
confirm(){
  local d="${2:-n}" a p
  [ "$d" = y ] && p="(Y/n)" || p="(y/N)"
  a=$(ask "$1 $p" "$d")
  [[ "$a" =~ ^[Yy] ]]
}

# 框宽固定 58 列. 每行按显示宽度补齐后再包边框 ——
# 手写空格对不齐, 因为 CJK 占 2 列而框线字符占 1 列.
BOX_W=58
_row(){ # _row "<内容>" [颜色代码]
  local txt="$1" col="${2:-}" pad
  pad=$(( BOX_W - $(_dispw "$txt") ))
  [ "$pad" -lt 0 ] && pad=0
  if [ -n "$col" ]; then printf '│\033[%sm%s\033[0m%*s│\n' "$col" "$txt" "$pad" ""
  else printf '│%s%*s│\n' "$txt" "$pad" ""; fi
}
_sep(){ printf '│'; printf '─%.0s' $(seq $BOX_W); printf '│\n'; }
_top(){ printf '╔'; printf '─%.0s' $(seq $BOX_W); printf '╗\n'; }
_bot(){ printf '╚'; printf '─%.0s' $(seq $BOX_W); printf '╝\n'; }

# 菜单条目：中文名、英文名、耗时三列各自按显示宽度补齐.
# 手写空格必然错位 —— 中文占 2 列,"~10 min" 这种右列一长就把右边框顶出去.
_item(){ # _item <编号> <中文> <英文> [耗时]
  _row "$(printf '  %2s. %s %s %s ' "$1" "$(_pad "$2" 12)" "$(_pad "$3" 25)" "$(_rpad "${4:-}" 12)")"
}

banner(){
  local iface cc shaper ram cores tuned rmem wmem
  iface=$(detect_iface)
  cc=$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null)
  shaper=$(r=$(tc_rate_mbit "$(tc class show dev "$iface" 2>/dev/null)") && echo "${r}Mbit")
  ram=$(detect_ram_mb); cores=$(detect_cores)
  rmem=$(read_tcp_buffer_max net.ipv4.tcp_rmem)
  wmem=$(read_tcp_buffer_max net.ipv4.tcp_wmem)
  [ -f "$SYSCTL_FILE" ] && tuned="Tuned" || tuned="Stock"
  clear 2>/dev/null || true
  echo
  _top
  _row "$(printf '  tcpfit - VPS TCP Optimization%s ' "$(_rpad "v$VERSION" 23)")" '0;32'
  _row "  本脚本由 kylin010 编写和维护"
  _row "  上游 Kylin010/tcpfit；本分支 P0me1oo/tcpfit-x"
  _row "  VPS 补货频道  t.me/vpskuaibu"
  _row "  VPS 测评数据  spacevps.cc"
  _sep
  _row "   0. Exit"
  _item 1 "国际线路调优" "International tuning"   "~10 min"
  _item 2 "优化线路调优" "Optimized route tuning"
  _item 3 "基础调优" "Base tuning only"          "~1 min"
  _item 4 "拐点测试" "Policer sweep"             "~8 min"
  _item 5 "加 swap"  "Add swap (low-memory box)"
  _sep
  _item 6 "查看状态" "Status"
  _item 7 "端口验证" "Verify port capability"    "~1 min"
  _item 8 "回滚改动" "Rollback all changes"
  _item 9 "检查更新" "Check for updates"
  _item 10 "调优存档" "Tuning archives"
  _row "  11. 卸载 tcpfit / Uninstall"
  _bot
  printf "  %-9s %s core / %s MB / %s\n" "Machine" "$cores" "$ram" "$(uname -r)"
  printf "  %-9s cc=%s  shaper=%s  buf(rx/tx)=%s/%s  " "Network" "${cc:-?}" "${shaper:-none}" "$rmem" "$wmem"
  [ "$tuned" = Tuned ] && printf "${green}%s${plain}\n" "$tuned" || printf "${yellow}%s${plain}\n" "$tuned"
  local _stats; _stats=$(telemetry_line)
  [ -n "$_stats" ] && printf "  %-9s %s\n" "Runs" "$_stats"
}

# 国际线路调优自动流程.
# 设计原则：所有要用户回答的东西集中在最前面（3 个问题）, 确认之后一路跑到底不再打断；
# 执行阶段的日志用英文（都是参数名和数值, 中英混排反而看不清）, 结论用中文.
wizard(){
  need_root
  take_lock
  migrate_legacy
  local WIZARD=1 ARCH_INCLUDE_SWEEP=0
  local ARCH_ROLE="" ARCH_BW="" ARCH_RTT="" ARCH_PEER=""
  local ram; ram=$(detect_ram_mb)
  echo
  echo "  ── 国际线路调优 ──"
  echo
  rule
  echo "  开始前的说明"
  echo
  echo "  改动前会把当前配置完整备份到"
  echo "      $(_c '1' "$SNAPSHOT")"
  echo "  包含拥塞控制、全部缓冲区参数、qdisc、路由等原始值."

  # 协议族: 默认 IPv4; 纯 v6 机器自动走 v6; 双栈才问.
  # 每个分支只说跟这台机器有关的话.
  # 早期版本在这里无条件打一句"测速默认走 IPv4. 检测不到 IPv4 会走 IPv6." ——
  # 本意是说明规则, 但纯 v4 机器(绝大多数)看到的就只有这一行, 而且没有任何一行
  # 确认"检测到了 v4". tcpfit 其他输出全是状态行, 用户就把"检测不到 IPv4"
  # 读成了对自己机器的判定, 以为脚本认错了. 有用户真这么报过.
  # 「没有 v4」不等于「有 v6」. 早期这里只判 v4, 判不出来就直接切 -6 ——
  # 于是 v4/v6 都检测不到的机器被切到一个根本不存在的协议族上, 之后每个节点
  # ping 都失败, 最后死在"公共测速服务器暂时都不可用". 客户实报过.
  if ! have_ipv4; then
    if have_ipv6; then
      IP_FAMILY="-6"
      ok "本机没有 IPv4, 测速走 IPv6"
    else
      # 两边都判不出来时保持默认的 -4: 绝大多数机器是纯 v4, 检测失误的可能
      # 远高于"真的两个都没有". 切到 v6 是必然失败, 留在 v4 至少还有机会.
      warn "检测不到 IPv4 也检测不到 IPv6 的默认路由, 仍按 IPv4 测速."
      warn "  若后面选不到对端, 用 --peer 手动指定."
    fi
  elif have_ipv6; then
    echo
    echo "  本机是 IPv4 + IPv6 双栈, 测速默认走 IPv4."
    echo
    local fam
    while true; do
      fam=$(ask "  用 v4 还是 v6？（回车 = v4）" "v4")
      case "$fam" in
        v4|V4|4) IP_FAMILY="-4"; break ;;
        v6|V6|6) IP_FAMILY="-6"; break ;;
        *) warn "  请输入 v4 或 v6" ;;
      esac
    done
    ok "测速走 IPv${IP_FAMILY#-}"
  fi

  # iperf3 单独放在最前面确认 —— 两个原因:
  #   1) 装包是会改系统的操作, 不该在用户点头之前做
  #   2) 选对端那一步要用 iperf3 做占线探测, 所以必须在三个问题之前就位
  local HAVE_IPERF3=1 QN=3
  if command -v iperf3 >/dev/null 2>&1; then
    echo "  iperf3 已经安装 $(iperf3 --version 2>/dev/null | awk 'NR==1{print $2}')"
  else
    echo "  确认带宽之前, tcpfit 需要安装 iperf3 才可以正常运行."
    echo
    if confirm "  安装？" y; then
      # 装之前先看锁, 被占的话直接说清楚, 别让用户干等一轮 apt 超时
      local _holder; _holder=$(apt_lock_holder)
      if [ -n "$_holder" ]; then
        set -- $_holder
        warn "包管理器正被 PID $1（$2, 已运行 $3）占用, 先不装了."
      else
        echo "    ────────────────────────────────────────"
        if   command -v apt-get >/dev/null; then apt-get update -qq && apt-get install -y iperf3
        elif command -v dnf     >/dev/null; then dnf install -y iperf3
        elif command -v yum     >/dev/null; then yum install -y epel-release; yum install -y iperf3
        # Alpine 的 busybox timeout/pkill 功能不全, 一并装 GNU 版
        elif command -v apk     >/dev/null; then apk add iperf3 coreutils procps
        else warn "认不出包管理器, 请手动安装 iperf3"; fi
        echo "    ────────────────────────────────────────"
      fi
    fi
    if command -v iperf3 >/dev/null 2>&1; then
      ok "iperf3 $(iperf3 --version 2>/dev/null | awk 'NR==1{print $2}') 已就绪"
    else
      # 不中止 —— 基础调优(BBR/缓冲区/起步)完全不依赖 iperf3, 那也是收益最大的一部分.
      # 少掉的是: 实测带宽、扫拐点、验证吞吐.
      HAVE_IPERF3=0; QN=2
      explain_pkg_failure
      warn "所以这次只能做基础调优:"
      warn "  不能实测带宽(要你手填)、不能扫限速器拐点、不能验证吞吐."
      warn "  基础调优本身照做, 那是收益最大的一部分."
      warn "  装上 iperf3 之后重跑一次 $(disp), 就能补上剩下的."
    fi
  fi

  # ping 单独检查, 【不能】嵌进上面 iperf3 的 else 分支里 ——
  # 实测有机器装了 iperf3 却没有 ping(Ubuntu 22.04 精简镜像), 那样会走进
  # "iperf3 已经安装" 那一支, 永远问不到 ping.
  # 缺 ping 的后果: auto_pick_peer 靠它给 18 个节点排延迟, 全拿不到就返回空,
  # 向导最后报 "公共测速服务器暂时都不可用" —— 那句在甩锅给无辜的对端.
  # ping 存在【不代表能用】: GNU inetutils 版不认 -4, 会让自动选对端全军覆没.
  # 这个分支必须在"没有 ping"之前判不了(没装时 ping_variant 返回 none),
  # 所以放在后面, 等 ping 确实存在时再看它是哪一种.
  if [ "$HAVE_IPERF3" = 1 ] && command -v ping >/dev/null 2>&1; then
    check_ping_variant || true
  fi
  if [ "$HAVE_IPERF3" = 1 ] && ! command -v ping >/dev/null 2>&1; then
    echo
    echo "  自动挑选测速对端需要 ping, 本机没有."
    echo
    if confirm "  安装 ping？" y; then
      echo "    ────────────────────────────────────────"
      if   command -v apt-get >/dev/null; then apt-get update -qq && apt-get install -y iputils-ping
      elif command -v dnf     >/dev/null; then dnf install -y iputils
      elif command -v yum     >/dev/null; then yum install -y iputils
      elif command -v apk     >/dev/null; then apk add iputils
      else warn "认不出包管理器, 请手动安装 ping"; fi
      echo "    ────────────────────────────────────────"
    fi
    if command -v ping >/dev/null 2>&1; then
      ok "ping 已就绪"
    else
      warn "本机缺少 ping, 无法自动选择对端."
      warn "  安装 iputils-ping, 或在下一步手动填写对端."
    fi
  fi

  # ── 1/3 带宽 ────────────────────────────────────────────────────────────
  step "1/${QN}  确认带宽"
  echo
  echo "    你这台机器的带宽是多少 Mbps？常见 100 / 200 / 300 / 500 / 1000."
  echo
  if [ "$HAVE_IPERF3" = 1 ]; then
    printf "    %s建议手动输入. %s回车会在执行阶段现场实测一个估值……\n" "$yellow" "$plain"
    echo "    跳过扫描. 填 0 表示不做整形（端口没有限速器时选这个）."
    echo "    已经知道限速值？输入 m 跳过拐点扫描直接指定"
  else
    printf "    %s没有 iperf3, 必须手动填一个数字.%s\n" "$yellow" "$plain"
  fi
  echo
  # MANUAL_RATE 的三种状态：""=正常扫描 / 数字=直接按该值整形 / "off"=完全不整形
  local bw MANUAL_RATE=""
  while true; do
    bw=$(ask "  带宽 Mbps" "")
    case "$bw" in
      "")      [ "$HAVE_IPERF3" = 0 ] && { warn "  没有 iperf3, 无法实测, 请手动填一个数字"; continue; }
               bw=auto; break ;;                       # 回车 → 执行阶段实测
      0)       MANUAL_RATE=off; bw=auto; break ;;      # 0 → 不整形, 带宽仍需实测
      m|M)                                             # m → 跳到限速值那一问
        while true; do
          MANUAL_RATE=$(ask "  限速值 Mbit" "")
          [ -z "$MANUAL_RATE" ] && { warn "  请填一个数字, 0 表示不做整形"; continue; }
          [ "$MANUAL_RATE" = 0 ] && { MANUAL_RATE=off; bw=auto; break; }
          if { [ "$MANUAL_RATE" -gt 0 ] && [ "$MANUAL_RATE" -le 100000 ]; } 2>/dev/null; then
            bw="$MANUAL_RATE"; break                   # 限速值同时作为算 BDP 的带宽基准
          fi
          warn "  请输入一个正整数（单位 Mbit）, 或 0 表示不做整形"
        done
        break ;;
      *)
        { [ "$bw" -gt 0 ] && [ "$bw" -le 100000 ]; } 2>/dev/null && break
        warn "  请输入一个正整数（单位 Mbps）, 或 m / 0" ;;
    esac
  done

  # ── 2/3 对端 ────────────────────────────────────────────────────────────
  # 没有 iperf3 就没有对端可言, 整段跳过, 且强制不做整形
  local peer="(不需要)"
  if [ "$HAVE_IPERF3" = 0 ]; then
    MANUAL_RATE="${MANUAL_RATE:-off}"
  else
  step "2/${QN}  确认测速对端"
  echo
  echo "    拐点扫描需要一台对端机器跑 iperf3 服务端."
  echo
  echo "    A) 直接回车 —— 用公共节点（默认）"
  echo "       由以下厂商免费提供, 测试流量会发往它们："
  echo "           Leaseweb / Clouvider / OVH"
  echo "           完整列表见 iperf3serverlist.net"
  echo
  echo "    B) 用你自己的另一台机器"
  echo "       在那台机器上执行这两条："
  printf "           %sapt install -y iperf3%s    # 装 iperf3；已装过会跳过, 不会重装\n" "$green" "$plain"
  printf "           %siperf3 -s%s                # 启动服务端, 默认监听 5201 端口\n" "$green" "$plain"
  echo "       然后在下面填那台机器的 IP, 例如  1.2.3.4"
  printf "       %s本脚本默认连 5201 端口%s；对端换了端口的话填  IP:端口  形式. \n" "$yellow" "$plain"
  echo "       对端要选离本机近的."
  echo
  while true; do
    peer=$(ask "  对端 IP / 域名（回车=公共节点）" "")
    if [ -z "$peer" ]; then
      # 不要断言"服务器不可用" —— 失败原因也可能在本机(缺 ping、协议族选错、出站被封),
      # auto_pick_peer 已经把真实原因打在上面了, 这里只说结果.
      local picked; picked=$(auto_pick_peer) || die "没能自动选出对端, 在上一步手动填一个" 2
      peer="${picked%:*}"; PEER_PORT="${picked##*:}"
      break
    fi
    # 拆主机和端口. 不能只按"最后一个冒号"拆 —— IPv6 地址本身满是冒号:
    #   2001:db8::1  会被拆成 主机=2001:db8: 端口=1, 然后拿着错主机错端口继续跑, 静默出错.
    # 五种形式都要认（不能只收带端口的, 界面上就是教用户填 1.2.3.4 这种裸地址）:
    #   1.2.3.4 / example.com        → 默认 5201
    #   1.2.3.4:5202 / host:5202     → 拆
    #   2001:db8::1                  → 裸 v6, 默认 5201
    #   [2001:db8::1]:5202           → 剥方括号再拆
    # 顺序有讲究: [v6]:port 必须排在 [v6] 前面, 裸 v6 (两个以上冒号) 必须排在 host:port 前面.
    PEER_PORT=5201
    case "$peer" in
      \[*\]:*) PEER_PORT="${peer##*]:}"; peer="${peer%%]:*}"; peer="${peer#\[}" ;;
      \[*\])   peer="${peer#\[}"; peer="${peer%\]}" ;;
      *:*:*)   : ;;
      *:*)     PEER_PORT="${peer##*:}"; peer="${peer%:*}" ;;
    esac
    if ! is_posint "$PEER_PORT" 1 65535; then
      warn "端口必须是 1-65535 之间的整数（IPv6 地址请写成 [地址]:端口）"; echo; continue
    fi
    # 手填的对端当场验一次可达性. 打错 IP 的话不该等到执行阶段才发现 ——
    # 那时前面三个问题都白填了, 而且已经改过 sysctl.
    printf '    检查 %s:%s … ' "$peer" "$PEER_PORT" >&2
    if probe_port "$peer" "$PEER_PORT" 6; then
      printf '%s\n' "$(_c '0;32' '可达')" >&2
      break
    fi
    printf '%s\n' "$(_c '0;31' '连不上')" >&2
    echo "      常见原因: 对端没在跑 iperf3 -s / 端口填错 / 防火墙挡了 / IP 打错"
    echo "      回车可以改用公共节点."
    echo
  done
  fi

  # ── 3/3 用途 ────────────────────────────────────────────────────────────
  step "${QN}/${QN}  机器用途"
  echo
  echo "    1) 代理 / 加速        并发连接多, 缓冲区取保守值（最常见）"
  echo "    2) 大文件传输 / 备份  少数大流, 缓冲区取激进值"
  echo
  local rc role
  rc=$(ask "  选择" "1")
  case "$rc" in 2) role=bulk ;; *) role=proxy ;; esac

  # ── 确认 ────────────────────────────────────────────────────────────────
  echo
  rule
  echo "  确认"
  echo
  if [ "$bw" = auto ]; then
    _conf "带宽" "自动实测（执行阶段测）"
  elif [ -n "$MANUAL_RATE" ]; then
    _conf "带宽" "${bw} Mbps"                      # 手填时余量无意义, 不显示
  else
    _conf "带宽" "${bw} Mbps        整形安全余量 $(calc_margin "$bw") Mbit"
  fi
  case "$MANUAL_RATE" in
    "")  _conf "整形" "实测拐点后自动决定" ;;
    off) _conf "整形" "不做整形" ;;
    *)   _conf "整形" "${MANUAL_RATE} Mbit" ;;
  esac
  [ "$HAVE_IPERF3" = 1 ] && _conf "对端" "${peer}:${PEER_PORT}"
  _conf "用途" "$([ "$role" = bulk ] && echo '大文件传输 / 备份' || echo '代理 / 加速')"
  if [ "$HAVE_IPERF3" = 1 ]; then _conf "iperf3" "$(iperf3 --version 2>/dev/null | awk 'NR==1{print $2}')"
  else _conf "iperf3" "无, 只做基础调优"; fi
  _conf "安装位置" "$SELF_PATH"
  echo
  if [ -n "$MANUAL_RATE" ]; then _conf "预计耗时" "约 1 分钟"
  else                              _conf "预计耗时" "约 10 分钟"; fi
  if [ -n "$MANUAL_RATE" ]; then _conf "预计流量" "很少"
  elif [ "$bw" = auto ]; then   _conf "预计流量" "带宽实测后才能估"
  else
    _conf "预计流量" "约 $(estimate_traffic_gb "$bw") GB"
    _conf ""         "先测一档判断有没有限速器, 没有就到此为止"
  fi
  # 2G 以上扫描代价陡增, 且代理场景的实际流量通常远达不到端口上限.
  # 只提醒, 不阻止 —— 用户可能就是要为大流量场景调.
  if [ -z "$MANUAL_RATE" ] && [ "$bw" != auto ] && [ "$bw" -gt 2000 ] 2>/dev/null; then
    echo
    warn "带宽 ${bw} Mbps 超过 2000, 拐点扫描代价很高."
    echo "      代理场景下实际流量通常远达不到这个值, 整形器很可能从不触发."
    echo "      想跳过的话, 重跑时带宽那一问填 0."
  fi
  rule
  confirm "  开始调优？" y || { info "已取消, 未做任何改动"; return 0; }

  # ══ 执行阶段：全自动, 不再有任何提问 ══════════════════════════════════
  traffic_mark
  printf '\n  %s════ Running ═══════════════════════════════════════════%s\n' "$bold" "$plain"

  printf '\n  %s[1/5] Base tuning%s\n' "$bold" "$plain"
  if [ "$bw" = auto ]; then
    info "Probing bandwidth (4 streams + pacing, ~15s)..."
    bw=$(probe_bandwidth "$peer" "$(detect_iface)") || die "bandwidth probe failed" 2
    ok "Measured ~${bw} Mbps"
  fi
  # 小带宽 policer 上 initcwnd 32 的首轮突发会直接打穿令牌桶.
  # 三台 10-20M 真机都表现为首秒重传、后续吞吐逐秒下降;
  # 向导对 <=100M 保留内核默认值. 显式 tune 命令的旧行为不变.
  if [ "$bw" -le 100 ] 2>/dev/null; then
    info "Low-bandwidth path: keeping the kernel default initcwnd"
    cmd_tune --role "$role" --bw "$bw" --no-initcwnd || die "base tuning failed"
  else
    cmd_tune --role "$role" --bw "$bw" || die "base tuning failed"
  fi
  ARCH_PEER="$peer"

  # 这四个必须在所有分支之前声明. set -u 下, 只要有一条路径没赋值,
  # 结尾传给 wizard_result 时就是 unbound variable —— v0.3.8 的"未检测到限速器"
  # 和"填 0 不整形"两条路都踩了这个(GitHub #1 #2).
  local knee="" rate="" margin="" no_knee=""

  # 手动指定了限速值（或选了不整形）→ 路径验证和拐点扫描都没有意义, 直接跳到应用
  if [ -n "$MANUAL_RATE" ]; then
    printf '\n  %s[2/3] Apply shaping%s\n' "$bold" "$plain"
    if [ "$MANUAL_RATE" = off ]; then
      cmd_shape --off
      rate=""
    else
      cmd_shape --rate "$MANUAL_RATE"
      rate="$MANUAL_RATE"
      info "Cooling down 15s before verification"
      sleep 15
    fi
    printf '\n  %s[3/3] Verify%s\n' "$bold" "$plain"
    command -v iperf3 >/dev/null && verify_measure "$peer" || warn "no iperf3, throughput not verified"
    wizard_archive
    wizard_result "$bw" "$rate" "$knee" "$margin" "$ram"
    return 0
  fi

  printf '\n  %s[2/5] Path quality check%s\n' "$bold" "$plain"
  info "Probing at 40% of ${bw} Mbps -- far below any policer."
  echo "    Retransmits at this rate would mean the link itself is lossy."
  local v; v=$(validate_peer "$peer" "$bw" "$(detect_iface)")
  case "$v" in
    clean:*) ok "Path clean (retrans ${v#clean:})" ;;
    # 链路本身丢包只会让拐点读低一点, 数据仍然有效 —— 警告后照跑, 不打断
    dirty:*) warn "Link is lossy (retrans ${v#dirty:}). The knee may read low; sweep continues." ;;
    slow:*)  warn "Peer only reached ${v#slow:} Mbps. Sweep will decide whether to abort." ;;
    *)       warn "Path check failed; continuing anyway." ;;
  esac

  printf '\n  %s[3/5] Policer sweep%s\n' "$bold" "$plain"
  local sweep_rc=0
  cmd_sweep --peer "$peer" --nominal "$bw" || sweep_rc=$?
  # rc=3 是"扫完了但没有可用拐点"(没限速器/超上限/超范围), 结果文件是这轮写的, 可以读.
  # 其他非 0 是这轮压根没跑成, 结果文件已被清空, 不要去读.
  [ "$sweep_rc" = 0 ] || [ "$sweep_rc" = 3 ] || warn "sweep failed, shaping skipped"

  local out_of_range="" above_cap=""
  if { [ "$sweep_rc" = 0 ] || [ "$sweep_rc" = 3 ]; } && [ -f "$STATE_DIR/sweep.result" ]; then
    ARCH_INCLUDE_SWEEP=1
    no_knee=$(awk -F= '/^NO_KNEE/{print $2}' "$STATE_DIR/sweep.result")
    out_of_range=$(awk -F= '/^OUT_OF_RANGE/{print $2}' "$STATE_DIR/sweep.result")
    above_cap=$(awk -F= '/^ABOVE_CAP/{print $2}' "$STATE_DIR/sweep.result")
    knee=$(awk -F= '/^KNEE/{print $2}'      "$STATE_DIR/sweep.result")
    rate=$(awk -F= '/^RECOMMEND/{print $2}' "$STATE_DIR/sweep.result")
    [ -n "$knee" ] && [ -n "$rate" ] && margin=$(( knee - rate ))
  fi

  printf '\n  %s[4/5] Apply shaping%s\n' "$bold" "$plain"
  # 上一轮如果应用过整形, sweep 结束时 qdisc_restore 会把那份配置原样装回来.
  # 所以"这次没检测到限速器"必须【主动移除】, 否则网卡上还挂着上次的限速值,
  # 而结果页写着"整形 未设置" —— 屏幕和实际不一致.
  # 只在 no_knee(确信没有限速器) 时移除; 扫描失败/超范围时并不知道有没有限速器,
  # 保留上次的配置更安全, 但要说清楚.
  if [ -n "$rate" ]; then cmd_shape --rate "$rate"
  elif [ -n "$out_of_range" ]; then
    info "policer present but knee not located in range, shaping skipped"
    [ -x "$QDISC_SCRIPT" ] && warn "上次的整形保留未动（本次没测准）"
  elif [ -n "$above_cap" ]; then
    info "unshaped throughput exceeds the sweep cap, shaping skipped"
    if [ -x "$QDISC_SCRIPT" ]; then
      warn "超过扫描上限, 无法判断限速器；上次的整形保留未动"
    else
      info "当前没有旧整形, 网卡立即切换为纯 fq"
      cmd_shape --off
    fi
  elif [ -n "$no_knee" ]; then
    if [ -x "$QDISC_SCRIPT" ]; then
      info "本次未发现限速器, 正在移除上次的整形"
    else
      info "未发现限速器, 当前网卡立即切换为纯 fq"
    fi
    cmd_shape --off
  else
    warn "no knee measured, shaping skipped"
    [ -x "$QDISC_SCRIPT" ] && warn "上次的整形保留未动（本次没测出结果）"
  fi

  printf '\n  %s[5/5] Verify%s\n' "$bold" "$plain"
  # 扫描的丢包档会耗尽服务商 policer 的令牌. 立即验证时第一条流会
  # 带着 1%-3% 的残余重传, 而几秒后的第二条流是 0. 和扫描前一样等待
  # 15s, 让验证测的是最终整形效果, 不是上一档的残留状态.
  if [ -n "$rate" ] || [ -n "$out_of_range" ] || [ -n "$above_cap" ]; then
    info "Cooling down 15s before verification"
    sleep 15
  fi
  command -v iperf3 >/dev/null && verify_measure "$peer" || warn "no iperf3, throughput not verified"

  wizard_archive
  wizard_result "$bw" "$rate" "$knee" "$margin" "$ram" "$no_knee" "$out_of_range" "$above_cap"
}

# 两条向导路径都在最终整形和验证后保存; archive_write 从机器读取实际状态.
# 未扫描或扫描失败时 ARCH_INCLUDE_SWEEP=0, 不把旧扫描写成本轮结果.
wizard_archive(){
  archive_save "wizard-${ARCH_BW}M-rtt${ARCH_RTT}" >/dev/null 2>&1 ||
    warn "调优已生效, 但存档没建成 —— rollback 仍可用($(disp) rollback), 存档功能可稍后手动 $(disp) archive save"
}

# 结果段落. 正常流程和"手动指定整形值"两条路径共用, 避免两份重复的排版代码.
wizard_result(){   # wizard_result <带宽> <整形值> <拐点> <余量> <内存MB> [无拐点] [超范围] [超上限]
  local bw="${1:-}" rate="${2:-}" knee="${3:-}" margin="${4:-}" ram="${5:-0}" no_knee="${6:-}" oor="${7:-}" cap="${8:-}"
  local cur_shape=""
  printf '\n  %s════ 结果 ══════════════════════════════════════════════%s\n' "$bold" "$plain"
  echo
  if [ -n "$knee" ]; then
    _conf "实测端口上限" "${knee} Mbit"
    _conf "安全余量"     "${margin} Mbit（按 ${bw}M 档位）"
    _conf "已应用整形"   "${rate} Mbit"
    echo
  elif [ -n "$rate" ]; then
    _conf "已应用整形"   "${rate} Mbit"
    echo
  else
    # 这几支都是"本次没应用整形". 但网卡上可能还挂着【上一轮】的限速 ——
    # 早期版本一律打"未设置", 屏幕和实际不一致. 这里直接读当前状态再报.
    cur_shape=$(tc_rate_mbit "$(tc class show dev "$(detect_iface)" 2>/dev/null)")
    if [ -n "$cur_shape" ]; then
      _conf "整形"       "${cur_shape} Mbit（上次的配置, 本次未改动）"
    else
      _conf "整形"       "未设置"
    fi
    if   [ -n "$oor" ];     then _conf "原因" "检测到限速迹象, 但未在扫描范围内定位到拐点"
    elif [ -n "$cap" ];     then
      _conf "原因" "不限速吞吐超过 ${cap} Mbit 扫描上限"
      _conf ""     "未判断是否有限速器, 不自动改整形"
    elif [ -n "$no_knee" ]; then _conf "原因" "扫描未发现限速器, 加整形只会限制自己"
    fi
    echo
  fi
  # verify 的判定也要按实况: 本次没应用但网卡上还有旧整形时, 目标值取那个,
  # 否则会给一台正被限速的机器说"这台没有应用整形".
  verify_verdict "${rate:-$cur_shape}"
  traffic_report
  echo
  echo "  本次改动和快照位置"
  echo "      $SYSCTL_FILE"
  [ -n "$rate" ] && echo "      $QDISC_UNIT"
  echo "      $SNAPSHOT"

  # 小内存且没 swap 才提. 内存够用或已有 swap 就完全不出现这一段.
  if [ "$ram" -le 1024 ] && ! not_blank "$(swapon --show 2>/dev/null)"; then
    step "swap"
    echo
    echo "    本机 ${ram} MB 内存且没有 swap. 跑代理时 TCP 缓冲区可能撑爆内存,"
    echo "    代理进程被系统杀掉."
    echo
    echo "    输入 1-20 的数字（单位 GB）, 推荐 1-4；回车 = 2；输入 0 = 不创建."
    echo
    # 这里必须自己校验. 直接把输入丢给 cmd_harden 的话, 非法值会触发它的 die,
    # 整个脚本跟着退出, 连"调优完成"都打不出来 —— v0.4.3 就是这么挂的.
    local sg
    while true; do
      sg=$(ask "  swap 大小 GB" "2")
      [ "$sg" = 0 ] && break
      if is_posint "${sg%[Gg]}" 1 20; then cmd_harden --swap "$sg"; break; fi
      warn "  请输入 1-20 之间的整数, 或 0 跳过"
    done
  fi
  echo
  ok "调优完成."
  echo
  echo "  ─────────────────────────────────────────────"
  echo "    菲比VPS补货频道：        t.me/vpskuaibu"
  echo "    星空VPS（免费API查阅机型）： spacevps.cc"
  echo "    问题反馈：               github.com/Kylin010/tcpfit/issues"
  echo "    合作：                   4496540pva@gmail.com"
  echo
}

menu_loop(){
  need_root
  take_lock
  self_install
  telemetry_ping
  while true; do
    banner
    echo
    local c; c=$(ask "  请选择 / Select [0-11]" "1")
    echo
    case "$c" in
      1) wizard
         # 跑完直接退出, 不回菜单. 回菜单要经过 banner 的 clear, 而 clear 发的是
         # \033[H\033[2J\033[3J —— 那个 3J 连滚动回滚缓冲一起清掉, 往上翻也找不回
         # 结果. 调优要跑十几分钟, 结果页面就是用户唯一要看的东西, 不能这么洗掉.
         echo
         echo "  要继续操作, 重新运行 ${bold}tcpfit${plain}"
         echo
         # 退出前必须清掉. 这十几分钟里用户随手按的键还躺在 tty 缓冲里,
         # 进程一退, 它们就被父 shell 读走当命令执行（实测 ls -la / whoami 真的跑了）.
         drain_tty
         exit 0 ;;
      2) cmd_return; local rc=$?; drain_tty; exit "$rc" ;;
      3) local r; r=$(ask "  用途 1) 代理/加速  2) 大文件传输" "1")
         local role=proxy; [ "$r" = 2 ] && role=bulk
         local b; b=$(ask "  带宽 Mbps (回车=自动探测)" "")
         if [ -n "$b" ]; then cmd_tune --role "$role" --bw "$b"
         else
           local p; if p=$(auto_pick_peer); then PEER_PORT="${p##*:}"; cmd_tune --role "$role" --bw auto --peer "${p%:*}"
           else warn "No peer available; specify bandwidth manually"; fi
         fi ;;
      4) local p; if p=$(auto_pick_peer); then
           PEER_PORT="${p##*:}"; p="${p%:*}"
           local b; b=$(ask "  带宽 Mbps" "")
           cmd_sweep --peer "$p" --nominal "$b"
           local rate; rate=$(awk -F= '/^RECOMMEND/{print $2}' "$STATE_DIR/sweep.result" 2>/dev/null)
           [ -n "$rate" ] && confirm "  应用 ${rate}Mbit 整形？" y && cmd_shape --rate "$rate"
         else warn "No peer available"; fi ;;
      5) if not_blank "$(swapon --show 2>/dev/null)"; then
           info "已有 swap: $(free -h | awk '/Swap/{print $2}'), 回车跳过；要再建就输入数字"
           local sg; sg=$(ask "  swap 大小 GB (1-20, 回车跳过)" ""); [ -n "$sg" ] && cmd_harden --swap "$sg"
         else
           echo "  输入 1-20 的数字（单位 GB）, 推荐 1-4；回车 = 2；输入 0 = 不创建."
           local sg; sg=$(ask "  swap 大小 GB" "2"); [ "$sg" != 0 ] && cmd_harden --swap "$sg"
         fi ;;
      6) cmd_status ;;
      7) local p; if p=$(auto_pick_peer); then PEER_PORT="${p##*:}"; cmd_verify --peer "${p%:*}"; else cmd_verify; fi ;;
      8) confirm "  确定回滚全部改动？" && cmd_rollback ;;
      9) cmd_update --from-menu ;;
      10) echo; archive_list; echo
         local a; a=$(ask "  回滚到哪个存档？(序号, 回车跳过)" "")
         if [ -n "$a" ]; then
           local f
           if f=$(archive_find "$a"); then
             confirm "  确定回滚到 $(archive_seq_of "$f") $(archive_name_of "$f")？" && archive_restore "$f"
           else
             local find_rc=$?
             [ "$find_rc" = 2 ] || warn "找不到存档: $a"
           fi
         fi ;;
      11) cmd_uninstall; exit $? ;;
      0) exit 0 ;;
      *) warn "Invalid selection" ;;
    esac
    echo
    drain_tty
    printf "  ${yellow}按任意键返回${plain}"
    read -rsn1 2>/dev/null </dev/tty || read -r 2>/dev/null </dev/tty || true
    echo
  done
}

# ── 入口 ────────────────────────────────────────────────────────────────────
usage(){ awk 'NR==1 {next} /^#/ {sub(/^# ?/, ""); print; next} {exit}' "$0"; }

# 支持优化线路调优协调进程加载同一份函数，以及隔离测试加载公共计算方法。
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
case "${1:-}" in
  return|return-tune) shift; cmd_return "$@" ;;
  return-recover) shift; cmd_return_recover "$@" ;;
  detect)   shift; cmd_detect "$@" ;;
  tune)     shift; cmd_tune "$@" ;;
  buffer)   shift; cmd_buffer "$@" ;;
  probe)    shift; cmd_probe "$@" ;;
  sweep)    shift; cmd_sweep "$@" ;;
  shape)    shift; cmd_shape "$@" ;;
  harden)   shift; cmd_harden "$@" ;;
  verify)   shift; cmd_verify "$@" ;;
  status)   shift; cmd_status "$@" ;;
  rollback) shift; cmd_rollback "$@" ;;
  archive|archives|snap) shift; cmd_archive "$@" ;;
  uninstall|remove) shift; cmd_uninstall "$@" ;;
  update)   shift; cmd_update "$@" ;;
  version)  echo "tcpfit $VERSION" ;;
  menu)     shift; menu_loop ;;
  "")       menu_loop ;;
  -h|--help|help) usage ;;
  *) die "未知命令: $1（-h 看用法）" ;;
esac
fi
