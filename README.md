# tcpfit-x 0.6.0

按每台机器实测推导的 TCP 调优工具. 不套用固定参数, 实测 BDP 与限速器拐点.

上游 [tcpfit](https://github.com/Kylin010/tcpfit) 由 [kylin010](https://github.com/Kylin010) 编写和维护。本分支由 [P0me1oo](https://github.com/P0me1oo) 维护，新增家宽主动接入的回国调优。

## 安装

```bash
curl -fsSL https://raw.githubusercontent.com/P0me1oo/tcpfit-x/main/install.sh | bash
tcpfit
```

安装器校验并安装主程序和同版本回国模块。主入口为 `/usr/local/bin/tcpfit`，模块位于 `/usr/local/lib/tcpfit/<版本>/`。菜单 `1` 为原版一键调优，`r` 为独立的回国调优。

从源码运行时，可在完整源码目录执行 `bash tcpfit.sh return`。单独复制一个主脚本时，回国模式需要下载对应版本的其他模块。

## 三种用法

| 用法 | 命令 |
|---|---|
| 一键跑 | `bash <(curl -fsSL .../main/tcpfit.sh)` |
| 装好后 | `tcpfit` |
| 子命令 | `tcpfit tune --role proxy --bw 500` |

## 菜单

```
   1. 一键调优   Auto-tune (recommended)  ~10 min
   r. 回国调优   Return-path tuning       ~6-10 min
   2. 基础调优   Base tuning only          ~1 min
   3. 拐点测试   Policer sweep             ~8 min
   4. 加 swap    Add swap (low-memory box)
   ────────────────────────────────────────────
   5. 查看状态   Status
   6. 端口验证   Verify port capability    ~1 min
   7. 回滚改动   Rollback all changes
   8. 检查更新   Check for updates
   9. 调优存档   Tuning archives
   u. 卸载 tcpfit
```

脚本不会自动更新. 装好之后跑的一直是装的那一版, 想升级用菜单 8 或 `tcpfit update` ——
它只检查, 发现新版本会问你要不要更新.

一键调优只问三个问题: 带宽、测速对端、机器用途. 确认之后跑到底不再打断.

带宽那一问支持四种输入:

| 输入 | 行为 |
|---|---|
| 数字 | 按该带宽推导缓冲区, 然后实测拐点 |
| 回车 | 现场实测带宽, 然后实测拐点 |
| `m` | 直接填限速值, 跳过拐点扫描 |
| `0` | 不做整形 |

## 子命令

```bash
tcpfit detect                                     # 机器画像
tcpfit probe    --peer <近处iperf3服务器>          # 探测可用带宽
tcpfit tune     --role proxy --bw 500             # 基础调优
tcpfit sweep    --peer <近处iperf3服务器> --nominal 500
tcpfit shape    --rate 510                        # 应用整形
tcpfit shape    --off                             # 移除整形, 保留基础调优
tcpfit harden   --swap 2G                         # 加 swap
tcpfit verify   --peer <近处iperf3服务器>          # 测速验证
tcpfit status                                     # 当前配置
tcpfit rollback                                   # 回滚全部改动
tcpfit update                                     # 检查更新
tcpfit archive list                               # 列出存档
tcpfit archive save "晚间配置"                     # 保存当前状态
tcpfit archive restore 0010                       # 恢复第 10 份存档
tcpfit archive rename 0010 "备用配置"              # 存档改名
tcpfit archive delete 0010                        # 删除指定存档
tcpfit uninstall --keep-archives                  # 卸载，保留存档和快照
```

## 多机（未上线）

多机编排还没在真实环境验证过, 暂时不建议使用. 下面的用法仅供参考.


```bash
cp inventory/servers.example.yml inventory/servers.yml
chmod 600 inventory/servers.yml
vi inventory/servers.yml

python3 orchestrator/fleet.py detect
python3 orchestrator/fleet.py tune
python3 orchestrator/fleet.py sweep
python3 orchestrator/fleet.py shape --auto
python3 orchestrator/fleet.py verify
```

选项: `--only 机器名` `--tag 标签` `-j 并发数` `--dry-run`.
临时执行任意命令: `fleet.py run -- uptime`.

## 它改了什么

| 类别 | 参数 |
|---|---|
| 拥塞控制 | `tcp_congestion_control=bbr` + `default_qdisc=fq` |
| 缓冲区 | `tcp_rmem` / `tcp_wmem` / `rmem_max` / `wmem_max` / `tcp_mem` |
| 窗口 | `tcp_window_scaling` / `tcp_moderate_rcvbuf` / `tcp_adv_win_scale` |
| 队列 | `netdev_max_backlog` / `netdev_budget` / `somaxconn` 等 |
| 连接 | `tcp_tw_reuse` / `tcp_fin_timeout` / `ip_local_port_range` 等 |
| 起步 | `tcp_slow_start_after_idle=0` / `initcwnd 32` |
| 出向整形 | HTB 全局上限 + fq 叶子 pacing |

共 32 个 sysctl 参数. 缓冲区和整形值按每台机器实测推导, 不是固定值.

## 拐点扫描怎么工作

先不限速跑一次, 看有没有东西在打你:

| 结果 | 动作 |
|---|---|
| 估算重传比低 | 未识别到限速器, 不整形 |
| 估算重传比高 | 从实测吞吐往上扫, 检查是否存在限速拐点 |
| 吞吐 > 2500 Mbit | 超出扫描上限, 不扫（可用 `--cap` 调整） |

拐点在"不限速吞吐"的**上面** —— 打穿限速器会让吞吐掉下来, 所以往上找.

## 回国调优

两个入口分别启动、独立完成。原版一键调优向公共测速节点发送数据，估测服务器出口能力；回国调优直接测试服务器到指定家宽的下载路径，不运行公共节点测速，也不要求先完成原版调优。

| 角色 | 系统 | 工作 |
|---|---|---|
| 调优端 | 常规 Linux，沿用原版的 systemd、iproute2 支持要求 | 保存和恢复配置，组织测速，推导和调整本机参数 |
| 测速端 | 常规 Linux、OpenWrt、iStoreOS | 安装缺少的 curl/iperf3，主动连接、接收数据和回报结果 |

OpenWrt、iStoreOS 本版只支持测速端。测速脚本使用 `/bin/sh`，无需 Python，不执行 sysctl、队列、路由修改，也不启动入站测速服务。

### 依赖与已有防火墙

| 角色 | 回国功能需要的工具 | 缺少时的处理 |
|---|---|---|
| 调优端 | Python 3、curl、iperf3；Python 只使用标准库 | 确认开始任务后补装缺少的工具 |
| 测速端 | `/bin/sh`、curl、iperf3 | 接入脚本补装缺少的 curl、iperf3 |

调优端沿用 Bash、iproute2（ip、tc、ss）、sysctl、systemctl 等系统工具。原版和回国入口共用的任务锁需要 `flock`，通常由 util-linux 提供；缺少时提示安装并停止。安装和更新需要 curl、sha256sum。

回国模式不再要求 OpenSSL 命令行工具，不主动安装 OpenSSL、证书包或防火墙工具。Debian 的依赖准备使用 `--no-install-recommends`；系统包自身的必要依赖仍由包管理器处理。已安装的测速依赖在任务结束后保留。

调优端按现状选择端口规则的处理方式：

- UFW 已启用：复用它现有的 iptables/ip6tables，或已有的 nftables 添加临时规则，不修改 UFW 配置文件。
- 未使用 UFW，但存在生效的旧版 iptables 规则：继续用原工具，同时兼容已有的原生 nftables 输入规则。
- 其他情况：优先使用已有 nftables，其次使用对应 IPv4/IPv6 的 iptables/ip6tables。
- 没有可用工具：直接继续任务，不安装防火墙，不添加测速端口的来源 IP 限制。开始确认和任务记录会明确显示这一状态。

程序不会启用原本关闭的 UFW。已有工具的规则读取或应用失败时，会明确报告错误并清理本任务的改动，不把工具故障当成没有防火墙。测速端不安装或调用防火墙管理工具。

### 开始测试

在调优服务器选菜单 `r`，或执行：

```bash
tcpfit return
# 已知带宽时建议填写；下例使用文档示例地址，请替换为服务器可达地址。
tcpfit return --server 203.0.113.10 --server-bw 5000 --client-bw 1000
```

启动前填写服务器地址、两个 TCP 端口、可选的两端标称带宽和用途，确认后会生成一条完整接入命令。在家宽设备上复制执行该命令，就会自动准备环境、配对并完成所有轮次，无须回到调优端确认。缺少依赖时需要 root 权限安装。

默认接入端口为 `5211`，测速端口为 `5212`，可用 `--control-port`、`--iperf-port` 修改。服务器地址必须能被家宽访问；云平台的安全组也要允许这两个 TCP 端口。家宽无需公网 IP 或入站端口映射。`-4`、`-6` 选择测试协议族，默认 IPv4；`--yes` 跳过已明确参数的开始确认；`--role proxy|bulk|mixed` 选择用途。

接入命令通过 HTTP 从调优端下载同版本的测速脚本，传递服务器地址、接入端口、测速端口和临时 token 四个参数，不嵌入程序。curl 或 wget 均直接下载调优端提供的脚本，开发版接入也不依赖已发布的版本标签。脚本下载、配对、控制指令和结果回报均使用明文 HTTP，不生成临时证书。

一次性随机 token 默认 600 秒有效，可用 `--token-ttl 30..1800` 设置。成功配对即撤销 token，后续会话绑定本次测速端的来源 IP。一个任务只能绑定一个测速端；再次接入会被拒绝，原任务继续。有可用防火墙工具时，临时 iperf3 端口只允许已配对的来源 IP；没有工具时保留会话配对，但不提供端口级来源限制。结束、断线或取消后停止临时服务、撤销凭据并删除本任务的规则。

两端应使用同版本脚本，每次任务都要使用本次生成的接入命令。更新调优端后重新生成命令，测速端会下载对应脚本。

iperf3 始终由家宽执行客户端 `-R`，服务器发送数据，分别使用单连接和四连接。没有家宽向服务器的大流量上传测试，也没有双向大流量测试。

### 自动流程与保留规则

标称出口、标称下载都可以留空。程序始终先实测当前路径可用带宽；这个结果不表示测出了服务器或家宽套餐上限。已填的标称值仅作能力与参数推导参考，不能代替实测。

1. 保存本机参数、实际队列、默认路由及 tcpfit 持久化文件。临时解除原整形速率限制，使用原版无限速 fq 探测方法。
2. 调用原版四连接带宽探测、带宽取整、缓冲区推导和完整基础调优。填写了标称值时，先取已填值中较小的一个，再与实测路径带宽取较大值作为推导参考；都未填则使用实测路径带宽。延迟自动采集，未取得时明确使用原版默认估值。不会另做缓冲区候选搜索或拥塞控制算法评选。
3. 调优前和基础候选各测三组单连接、四连接，沿用原版每次 10 秒的验证方法。两组都在相同的临时无限速 fq 条件下比较。单连接与四连接的吞吐中位数均未下降，且重传分档未变差、没有重复重传跳变时保留基础候选，否则恢复原基础配置。
4. 重传分档复用原版的 `<0.05%`、`<0.5%`、`<1%`、`>=1%`。跳变阈值为 `min(1%, max(0.1%, 5 × 基线估算重传比))`，三次中至少两次超过才认定为重复跳变。这些数值是估算重传比，不是丢包率。吞吐不下降的自动保留条件是本版明确增加的前后比较规则。
5. 最终对实际保留的配置验证，保存结果和完整配置。没有旧整形且最终配置与基础候选相同时，直接使用刚完成的三组有效测量；其他情况重新测量。

整形按以下规则处理，不运行完整拐点扫描：

| 原始状态或实测结果 | 最终处理 |
|---|---|
| 原本没有全局整形 | 不创建全局整形 |
| 三次四连接吞吐未全部超过旧上限 | 恢复原整形 |
| 稳定超过旧上限，扣除原版安全余量后仍高于旧值 | 仅验证这一个提高候选 |
| 候选重传验证通过、四连接吞吐中位数达到候选值的 75%，且三次均超过旧上限 | 用原版整形方法持久化提高值，再作最终验证 |
| 候选未通过或最终验证未通过 | 恢复原整形，不降低旧上限 |
| 测速失败、断线、取消、异常退出 | 恢复本次尚未确认的参数和临时队列状态 |

75% 是原版“偏低但可接受”的吞吐档位，90% 是原版正常档位。回国验证复用这些阈值，不把单次峰值当作提高已有上限的依据。

为保证恢复可靠，目前识别并保存 fq、fq_codel、内核默认 pfifo_fast、noqueue、mq 及其各个叶子，以及原版的单级 HTB + fq 整形。自定义出口 filter、多级 HTB、CAKE、TBF、硬件卸载等未支持结构会在改参数前明确拒绝。单独 fq 的有限 `maxrate` 属于每流上限，测试后原样恢复，不把它当作可提高的全局整形值。

### 指标与记录

| 指标 | 来源与定义 |
|---|---|
| 吞吐 Mbps | 家宽 iperf3 `sum_received.bits_per_second`，即实际送达的 TCP 数据吞吐 |
| 重传次数 | 服务器 iperf3 `sum_sent.retransmits`，TCP 重传次数，不是丢失的数据包比例 |
| 估算重传比 | `重传次数 × 1448 / 发送字节数 × 100%`，复用原版按固定 MSS 估算的比较口径 |
| 空载延迟 | 本轮下载前，测速端到服务器控制端口的 TCP 握手耗时均值 |
| 满载延迟 | 本轮 iperf3 下载期间，同一控制端口的 TCP 握手耗时均值 |

延迟使用 curl 的 `time_connect - time_namelookup`，每次新建连接；有效样本少于三个显示“未取得”，不填零。它不是 ICMP ping，也不包含整个 HTTP 请求耗时。

结果分别展示单连接、四连接的调优前、基础候选和最终配置吞吐、重传与延迟。三次测量取吞吐居中的那一次，并一起展示该次真实的重传和延迟。只有方向、连接数、时长、测速端和整形条件一致的前后测量才计算吞吐变化百分比；旧上限和最终新上限不同的两组数据不会冒充纯参数提升。

每次任务记录位于 `/var/lib/tcpfit/return/<时间-任务号>/`，包含 `before.json`、每次有效测量的 JSON、`result.json`、`final.json` 及事务恢复记录。`result.json` 还记录实际使用的防火墙工具及是否启用来源 IP 限制。原始 iperf3 的临时 cookie 不写入长期记录。认证失败、执行失败、缺少数据的测量不会参与计算，也不会使用公共节点结果替代。

原版 `pre-tune.snapshot`、`0000` 快照和 `rollback` 语义保留。新增任务前后存档出现在 `tcpfit archive list` 中；通过 `tcpfit archive restore <序号>` 恢复回国存档时，会恢复完整队列、文件权限和服务状态。保留这些存档时也要保留对应的 `return/` 记录目录。

两个调优入口和恢复入口共用任务锁，重复启动会提示等待。普通退出自动恢复并清理；协调进程被强制结束时由守护进程恢复。若重启、只读文件系统或权限问题导致恢复未完成，下一次调优会拒绝启动，先执行：

```bash
tcpfit return-recover
```

该命令只重试当前未完成的恢复，不抢占仍在运行的任务。程序升级要同时保留主脚本、回国模块及测量端脚本；卸载会一并删除已安装模块，`--keep-archives` 继续保留配置和结果。

## 回滚

```bash
tcpfit rollback                # 按快照逐项写回, 不是恢复默认
tcpfit rollback --purge-swap   # 同时删掉 harden 建的 /swapfile
tcpfit shape --off    # 只去掉整形
```

首次改动前自动存快照到 `/var/lib/tcpfit/pre-tune.snapshot`, 记录全部 32 项参数的原始值.

0.5.7 起，原始快照同时保留为 `0000 出厂状态`，不可改名或单独删除。
`archive restore 0000` 与 `rollback` 使用同一回滚流程；“出厂状态”指首次调优前的快照。
普通存档位于 `/var/lib/tcpfit/archives/`。基础调优后自动保存，一键调优则在最终整形、验证完成后保存。
序号可以输入 `10` 或 `0010`；名字含空格时请加引号。

恢复普通存档会同步 sysctl 启动配置和整形服务。有路由窗口设置时沿用 networkd-dispatcher hook；
缺少该目录会提示路由只能即时恢复并返回失败。恢复失败可能已经应用部分设置，请按提示检查后重试。

`tcpfit uninstall` 默认删除存档；需要保留则加 `--keep-archives`。
若回滚失败，卸载会停止并保留存档。卸载不删除 swap、iperf3 或 ping。

swap 默认不动 —— 删掉正在用的 swap 可能让机器立刻 OOM, 要一并撤销得显式加 `--purge-swap`.

改动只落在这些文件, 不碰 `/etc/sysctl.conf`:

```
/etc/sysctl.d/99-tcpfit.conf
/etc/systemd/system/tcpfit-qdisc.service
/usr/local/sbin/tcpfit-qdisc.sh
/etc/networkd-dispatcher/routable.d/50-tcpfit-initcwnd
/etc/modules-load.d/tcpfit-bbr.conf
/var/lib/tcpfit/
```

用了 `harden --swap` 还会创建 `/swapfile` 并往 `/etc/fstab` 加一行 —— 这两个 `rollback` 默认不动,
要一并撤销加 `--purge-swap`. 缺 iperf3 时经你确认后会用包管理器安装它.

## 已知限制

- 瓶颈在国际链路而非端口时, 整形不会带来提升, 但输出看起来一切正常
- 扫满区间没找到拐点时会把区间上界当成拐点, 这种情况用 `m` 手动指定
- 需要 Linux + systemd + iproute2. OpenVZ/LXC 上 `tc` 和 `initcwnd` 可能受限
- `sweep` 需要一台近处的 iperf3 对端

## 从 nettune 升级

老机器上的产物文件名还是 `nettune-*`, 新版本会自动检测并搬迁, 快照和 rollback 都保留. 直接跑新版即可.

## 许可证

[MIT](LICENSE)
