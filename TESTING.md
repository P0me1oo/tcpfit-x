# 开发与验证

## 本地回归

需要 Python 3 和 Bash。Windows 可使用 Git for Windows 的 Bash。

```bash
python -X utf8 -m unittest discover -s tests -v
bash -n tcpfit.sh
bash -n install.sh
sh -n tcpfit-client.sh
```

常规回归检查真实结果解析、错误与不完整数据拒绝、一次性配对、来源绑定、心跳中断、原版重传阈值、整形提高条件和路由恢复。Linux 集成测试默认跳过，不会在普通开发机上修改网络。

## Linux 隔离集成

需要 Linux root、Python 3、Bash、iproute2、iptables、util-linux。必须在独立网络命名空间中运行；测试会主动拒绝主机初始网络命名空间。

```bash
sudo env TCPFIT_LINUX_INTEGRATION=1 unshare --net \
  python3 -m unittest discover -s tests -v
```

集成测试只创建临时虚拟网卡与本地测试端口，覆盖以下行为：

- 单层 HTB + fq、定制 fq、fq_codel、mq 各叶子的保存和恢复。
- 只有已配对来源可以访问测试端口，删除临时规则后不残留任务链。
- 原版入口与手动恢复遵守相同任务锁。
- 协调进程被 SIGKILL 后，守护进程恢复队列、清理凭据与待恢复标记。
- 测速端被 SIGKILL 后，独立清理进程撤销本地凭据和任务锁，并报告异常退出。
- IPv6 路由到期倒计时变化不触发无意义回写；确需回写时使用 iproute2 接受的有效期格式。

还可同时检查安装、模块定位、校验失败时保留原入口以及完整更新。此测试另外隔离挂载命名空间，在临时文件系统中安装程序，以本地样本代替远程下载：

```bash
sudo env TCPFIT_INSTALL_INTEGRATION=1 TCPFIT_LINUX_INTEGRATION=1 \
  unshare --mount --net python3 -m unittest discover -s tests -v
```

## 双端实测

只验证接入、认证、反向下载和结果回报，可在常规 Linux 服务器执行：

```bash
sudo python3 tests/peer_smoke.py --server <服务器可达地址>
```

在另一端复制输出的接入命令。该检查分别运行短时单连接和四连接下载，结束后清理自己的端口规则；不应用系统调优参数。要验证完整流程，使用 `bash tcpfit.sh return`。

完整实测必须另外保存服务器现状，并在测试结束后按约定保留或恢复。核对运行参数、持久化文件、队列、默认路由、服务状态以及两端的临时进程和文件。路由比较应排除自动路由剩余寿命等动态字段。测速端缺少 tc 时，可通过 `ip -d link show` 核对队列类型，不要为了检查而改它的队列。

实机原始记录包含地址、配置和测量数据，留在设备的私有目录中，不提交到源码仓库。吞吐变化只证明当时条件下的测量差异；是否保留参数，以任务记录的完整吞吐和重传判定为准。

## 发布文件

版本号在 `tcpfit.sh`、`tcpfit-return.py`、`tcpfit-client.sh` 和 README 中保持一致。使用 UTF-8、LF 换行，重新生成并检查：

```bash
sha256sum tcpfit.sh install.sh tcpfit-return.py tcpfit-client.sh > SHA256SUMS
sha256sum -c SHA256SUMS
```

发布 `v<版本号>` 标签时，Release 附件应包含这四个运行文件和 `SHA256SUMS`。更新命令从 Release 下载，测速端 wget 回退从同一版本标签下载；缺文件或校验失败会停止安装或更新。
