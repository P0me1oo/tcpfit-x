#requires -Version 5.1
# Windows 测速端：只主动接入、接收测试数据和回报结果。
param(
    [string]$Server,
    [int]$ControlPort,
    [int]$IperfPort,
    [string]$Token,
    [string]$IperfPath,
    [switch]$Help
)

$TCPFIT_CLIENT_VERSION = '0.16.0'

function Initialize-TcpfitProcessJob {
    # 由系统在 PowerShell 被强制关闭时终止本任务的子进程，无需凭据文件或守护脚本。
    if ('Tcpfit.WindowsProcessJob' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
namespace Tcpfit {
    public sealed class WindowsProcessJob : IDisposable {
        [StructLayout(LayoutKind.Sequential)]
        struct BasicLimits {
            public long ProcessTime, JobTime;
            public uint Flags;
            public UIntPtr MinimumWorkingSet, MaximumWorkingSet;
            public uint ActiveProcesses;
            public UIntPtr Affinity;
            public uint Priority, Scheduling;
        }
        [StructLayout(LayoutKind.Sequential)]
        struct IoCounters {
            public ulong ReadOperations, WriteOperations, OtherOperations;
            public ulong ReadBytes, WriteBytes, OtherBytes;
        }
        [StructLayout(LayoutKind.Sequential)]
        struct ExtendedLimits {
            public BasicLimits Basic;
            public IoCounters Io;
            public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
        }
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern SafeFileHandle CreateJobObject(IntPtr attributes, string name);
        [DllImport("kernel32.dll", SetLastError = true)]
        static extern bool SetInformationJobObject(SafeFileHandle job, int kind, ref ExtendedLimits limits, uint length);
        [DllImport("kernel32.dll", SetLastError = true)]
        static extern bool AssignProcessToJobObject(SafeFileHandle job, IntPtr process);
        readonly SafeFileHandle handle;
        public WindowsProcessJob() {
            handle = CreateJobObject(IntPtr.Zero, null);
            if (handle.IsInvalid) throw new Win32Exception();
            var limits = new ExtendedLimits();
            limits.Basic.Flags = 0x2000;
            if (!SetInformationJobObject(handle, 9, ref limits, (uint)Marshal.SizeOf(limits))) {
                int error = Marshal.GetLastWin32Error();
                handle.Dispose();
                throw new Win32Exception(error);
            }
        }
        public void Add(Process process) {
            if (!AssignProcessToJobObject(handle, process.Handle) && !process.HasExited)
                throw new Win32Exception();
        }
        public void Dispose() { handle.Dispose(); }
    }
}
'@
}

function Start-TcpfitProcess($Context, [string]$File, [string]$Arguments) {
    $process = New-Object Diagnostics.Process
    $process.StartInfo.FileName = $File
    # 参数仅来自本脚本的固定选项、已验证的 IP、整数，不接收远端命令行。
    $process.StartInfo.Arguments = $Arguments
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.CreateNoWindow = $true
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.RedirectStandardError = $true
    $process.StartInfo.StandardOutputEncoding = [Text.Encoding]::UTF8
    $process.StartInfo.StandardErrorEncoding = [Text.Encoding]::UTF8
    $started = $false
    try {
        if (-not $process.Start()) { throw '无法启动测速工具' }
        $started = $true
        $Context.Job.Add($process)
        return @{ Process = $process; Output = $process.StandardOutput.ReadToEndAsync(); Error = $process.StandardError.ReadToEndAsync() }
    } catch {
        if ($started -and -not $process.HasExited) { $process.Kill() }
        $process.Dispose()
        throw
    }
}

function Stop-TcpfitProcess($Running) {
    if ($null -eq $Running) { return }
    try {
        if (-not $Running.Process.HasExited) { $Running.Process.Kill() }
        [void]$Running.Process.WaitForExit(5000)
    } finally { $Running.Process.Dispose() }
}

function Find-TcpfitIperf([string]$ExplicitPath) {
    if ($ExplicitPath) {
        $item = Get-Item -LiteralPath $ExplicitPath -ErrorAction Stop
        if ($item.PSIsContainer -or $item.Extension -ne '.exe') { throw '-IperfPath 必须指向 iperf3.exe' }
        return $item.FullName
    }
    $found = Get-Command iperf3.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { return $found.Source }
    if ($PSScriptRoot -and (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'iperf3.exe') -PathType Leaf)) {
        return (Join-Path $PSScriptRoot 'iperf3.exe')
    }
    # WinGet 安装后，当前 PowerShell 的 PATH 可能尚未刷新。
    foreach ($root in @($env:LOCALAPPDATA, $env:ProgramFiles)) {
        if (-not $root) { continue }
        $link = Join-Path $root 'Microsoft\WinGet\Links\iperf3.exe'
        if (Test-Path -LiteralPath $link -PathType Leaf) { return $link }
        $packages = Join-Path $root 'Microsoft\WinGet\Packages'
        if (Test-Path -LiteralPath $packages) {
            $packageDirs = Get-ChildItem -LiteralPath $packages -Directory -Filter 'ar51an.iPerf3_*'
            foreach ($directory in $packageDirs) {
                $binary = Get-ChildItem -LiteralPath $directory.FullName -Recurse -File -Filter iperf3.exe | Select-Object -First 1
                if ($binary) { return $binary.FullName }
            }
        }
    }
    return $null
}

function Get-TcpfitIperf($Context, [string]$ExplicitPath) {
    $binary = Find-TcpfitIperf $ExplicitPath
    if (-not $binary) {
        $winget = Get-Command winget.exe -CommandType Application -ErrorAction SilentlyContinue
        if (-not $winget) { throw '未找到 iperf3.exe 或 WinGet。请先安装 iperf3 并加入 PATH，或用 -IperfPath 指定路径；尚未配对。' }
        Write-Host '[*] 通过 WinGet 安装 iperf3'
        $install = $null
        try {
            $install = Start-TcpfitProcess $Context $winget.Source 'install --id ar51an.iPerf3 --exact --source winget --silent --accept-package-agreements --accept-source-agreements --disable-interactivity'
            $timer = [Diagnostics.Stopwatch]::StartNew()
            while (-not $install.Process.WaitForExit(200)) {
                if ($timer.Elapsed.TotalSeconds -gt 300) { throw 'WinGet 安装超时，尚未配对。请安装完成后重新接入。' }
            }
            if ($install.Process.ExitCode -ne 0) { throw ('WinGet 安装失败：' + $install.Output.Result + $install.Error.Result) }
        } finally { Stop-TcpfitProcess $install }
        $binary = Find-TcpfitIperf ''
        if (-not $binary) { throw '安装后仍未找到 iperf3.exe，请用 -IperfPath 指定路径；尚未配对。' }
    }
    $check = $null
    try {
        $check = Start-TcpfitProcess $Context $binary '--version'
        $timer = [Diagnostics.Stopwatch]::StartNew()
        while (-not $check.Process.WaitForExit(100)) {
            if ($timer.Elapsed.TotalSeconds -gt 5) { throw 'iperf3 版本检查超时，尚未配对' }
        }
        if ($check.Process.ExitCode -ne 0 -or $check.Output.Result -notmatch '^iperf 3\.') {
            throw 'iperf3.exe 无法正常运行，请检查版本及随程序附带的 DLL；尚未配对'
        }
    } finally { Stop-TcpfitProcess $check }
    return $binary
}

function Invoke-TcpfitApi($Context, [string]$Method, [string]$Endpoint, [string]$Body = '', [int]$Timeout = 20000) {
    $request = [Net.HttpWebRequest]::Create($Context.Base + $Endpoint)
    $request.Proxy = $null
    $request.AllowAutoRedirect = $false
    $request.KeepAlive = $false
    $request.Timeout = $Timeout
    $request.ReadWriteTimeout = $Timeout
    $request.Method = $Method
    $request.Headers['Authorization'] = $Context.Authorization
    $request.Headers['X-Tcpfit-Version'] = $TCPFIT_CLIENT_VERSION
    $request.ServicePoint.Expect100Continue = $false
    $response = $null
    $reader = $null
    try {
        if ($Method -eq 'POST') {
            $data = [Text.Encoding]::UTF8.GetBytes($Body)
            $request.ContentType = 'text/plain; charset=utf-8'
            $request.ContentLength = $data.Length
            $stream = $request.GetRequestStream()
            try { $stream.Write($data, 0, $data.Length) } finally { $stream.Dispose() }
        }
        try { $response = $request.GetResponse() }
        catch [Net.WebException] {
            if (-not $_.Exception.Response) { throw '无法连接调优端，连接失败或请求超时' }
            $response = $_.Exception.Response
        }
        $reader = New-Object IO.StreamReader($response.GetResponseStream(), [Text.Encoding]::UTF8)
        $text = $reader.ReadToEnd().Trim()
        if ([int]$response.StatusCode -ne 200) { throw ('{0}（HTTP {1}）' -f $text, [int]$response.StatusCode) }
        return $text
    } finally {
        if ($reader) { $reader.Dispose() }
        if ($response) { $response.Dispose() }
        $request.Abort()
    }
}

function Get-TcpfitLatency($Context) {
    $tcp = New-Object Net.Sockets.TcpClient($Context.Address.AddressFamily)
    try {
        # 字面 IP 无 DNS 时间；只计新连接的 TCP 握手，单位为秒。
        $timer = [Diagnostics.Stopwatch]::StartNew()
        $connect = $tcp.ConnectAsync($Context.Address, $Context.ControlPort)
        if (-not $connect.Wait(5000)) { throw 'TCP 握手超时' }
        $timer.Stop()
        $seconds = $timer.Elapsed.TotalSeconds.ToString('F9', [Globalization.CultureInfo]::InvariantCulture)
    } catch { return $null }
    finally { $tcp.Close() }
    # 每个采样周期同时发送认证心跳，保持与 Linux 测速端相同的断线检测。
    [void](Invoke-TcpfitApi $Context GET '/ping' '' 5000)
    return ($seconds + ' 0')
}

function Send-TcpfitIdleLatency($Context, [string]$Id) {
    $idle = @()
    for ($i = 0; $i -lt 5; $i++) { $idle += Get-TcpfitLatency $Context }
    [void](Invoke-TcpfitApi $Context POST "/latency/$Id/idle" ($idle -join "`n"))
}

function Invoke-TcpfitTest($Context, [string]$Id, [int]$Duration, [int]$Streams) {
    Write-Host ('[*] 测速：{0} 秒 × {1} 连接' -f $Duration, $Streams)
    Send-TcpfitIdleLatency $Context $Id
    $running = $null
    try {
        $arguments = '-{0} -c {1} -p {2} -R -P {3} -t {4} -J' -f $Context.Family, $Context.Address, $Context.IperfPort, $Streams, $Duration
        $running = Start-TcpfitProcess $Context $Context.Iperf $arguments
        $timer = [Diagnostics.Stopwatch]::StartNew()
        $nextSample = 1.0
        $loaded = @()
        while (-not $running.Process.WaitForExit(100)) {
            if ($timer.Elapsed.TotalSeconds -gt ($Duration + 25)) { throw 'iperf3 执行超时' }
            if ($timer.Elapsed.TotalSeconds -ge $nextSample) {
                $loaded += Get-TcpfitLatency $Context
                $nextSample = $timer.Elapsed.TotalSeconds + 1
            }
        }
        if ($running.Process.ExitCode -ne 0) { throw ('iperf3 执行失败（退出码 {0}）：{1}{2}' -f $running.Process.ExitCode, $running.Error.Result, $running.Output.Result) }
        [void](Invoke-TcpfitApi $Context POST "/latency/$Id/loaded" ($loaded -join "`n"))
        [void](Invoke-TcpfitApi $Context POST "/result/$Id" $running.Output.Result)
        Write-Host '[+] 本轮结果已回报，等待后续任务'
    } finally { Stop-TcpfitProcess $running }
}

function Invoke-TcpfitClient {
    Set-StrictMode -Version 2.0
    $ErrorActionPreference = 'Stop'
    if ($Help -or -not ($Server -or $ControlPort -or $IperfPort -or $Token)) {
        Write-Host '请在 PowerShell 中执行调优端生成的 Windows 接入命令。'
        Write-Host '用法: .\tcpfit-client.ps1 服务器IP 接入端口 测速端口 临时token [-IperfPath C:\路径\iperf3.exe]'
        return
    }
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw '本脚本仅用于 Windows 测速端' }
    $address = $null
    if ($Server -notmatch '^[a-fA-F0-9.:]+$' -or -not [Net.IPAddress]::TryParse($Server, [ref]$address)) { throw '接入地址必须是调优端生成的 IP 地址' }
    if ($ControlPort -lt 1024 -or $ControlPort -gt 65535 -or $IperfPort -lt 1024 -or $IperfPort -gt 65535 -or $ControlPort -eq $IperfPort) { throw '端口必须为 1024-65535 且互不相同' }
    if ($Token -cnotmatch '^[a-zA-Z0-9_-]{32}$') { throw '临时 token 格式无效，请重新复制完整接入命令' }
    $family = if ($address.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetworkV6) { 6 } else { 4 }
    $hostPart = if ($family -eq 6) { '[' + $address + ']' } else { $address.ToString() }
    $context = @{ Base = "http://${hostPart}:$ControlPort"; Address = $address; ControlPort = $ControlPort; IperfPort = $IperfPort; Family = $family; Authorization = ''; Job = $null; Iperf = '' }
    $mutex = $null
    $owned = $false
    $paired = $false
    $finished = $false
    $failure = '测速端收到取消信号'
    try {
        $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $mutex = New-Object Threading.Mutex($false, "Local\TcpfitClient-$sid")
        try { $owned = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $owned = $true }
        if (-not $owned) { throw '本机当前用户已有测速端任务，不能重复启动' }
        Initialize-TcpfitProcessJob
        $context.Job = New-Object Tcpfit.WindowsProcessJob
        $context.Iperf = Get-TcpfitIperf $context $IperfPath
        $context.Authorization = 'Pair ' + $Token
        Write-Host '[*] 正在连接调优端并认证'
        $reply = Invoke-TcpfitApi $context POST '/pair'
        if ($reply -cnotmatch '^OK ([a-f0-9]{48})$') { throw '调优端的配对响应无效' }
        $context.Authorization = 'Bearer ' + $Matches[1]
        $Token = ''
        $paired = $true
        Write-Host '[+] 已配对，后续自动测速；无需返回调优端操作'
        while ($true) {
            $reply = Invoke-TcpfitApi $context GET '/next'
            if ($reply -ceq 'WAIT') { Start-Sleep -Milliseconds 1000; continue }
            if ($reply -cmatch '^LATENCY ([a-f0-9]{16})$') {
                Write-Host '[*] 采集空载延迟'
                Send-TcpfitIdleLatency $context $Matches[1]
            } elseif ($reply -cmatch '^RUN ([a-f0-9]{16}) ([0-9]{1,3}) (1|4)$') {
                $id = $Matches[1]; $duration = [int]$Matches[2]; $streams = [int]$Matches[3]
                if ($duration -lt 1 -or $duration -gt 600) { throw '测试时长超出范围' }
                Invoke-TcpfitTest $context $id $duration $streams
            } elseif ($reply.StartsWith('DONE OK ')) {
                $finished = $true
                Write-Host ('[+] ' + $reply.Substring(8))
                return
            } elseif ($reply.StartsWith('DONE FAIL ')) {
                $finished = $true
                throw $reply.Substring(10)
            } elseif ($reply.StartsWith('FAIL ')) { throw $reply.Substring(5) }
            else { throw '无法识别调优端请求' }
        }
    } catch {
        $failure = $_.Exception.Message
        throw
    } finally {
        if ($context.Job) { $context.Job.Dispose() }
        if ($paired -and -not $finished) {
            try { [void](Invoke-TcpfitApi $context POST '/error' $failure 3000) } catch { }
        }
        $context.Authorization = ''
        if ($owned) { $mutex.ReleaseMutex() }
        if ($mutex) { $mutex.Dispose() }
    }
}

if ($MyInvocation.InvocationName -ne '.') { Invoke-TcpfitClient }
