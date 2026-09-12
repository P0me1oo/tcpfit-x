"""安装、校验失败和更新集成测试；仅在隔离挂载和网络命名空间中显式运行。"""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE_VERSION = re.search(r'^VERSION="([0-9.]+)"$', (ROOT / "tcpfit.sh").read_text(encoding="utf-8"), re.MULTILINE).group(1)
ENABLED = sys.platform.startswith("linux") and os.environ.get("TCPFIT_INSTALL_INTEGRATION") == "1"


@unittest.skipUnless(ENABLED, "仅在显式启用的 Linux 隔离挂载环境中运行")
class InstallIntegrationTests(unittest.TestCase):
    def test_install_module_resolution_and_atomic_checked_update(self):
        for kind in ("mnt", "net"):
            self.assertNotEqual(os.readlink("/proc/self/ns/" + kind), os.readlink("/proc/1/ns/" + kind), "必须使用 unshare --mount --net")
        subprocess.run(["mount", "--make-rprivate", "/"], check=True)
        subprocess.run(["mount", "-t", "tmpfs", "tmpfs", "/usr/local"], check=True)
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            fixture = directory / "files"
            fixture.mkdir()
            bin_dir = directory / "bin"
            bin_dir.mkdir()
            fake_curl = bin_dir / "curl"
            fake_curl.write_text('''#!/usr/bin/env python3
import os,pathlib,sys
root=pathlib.Path(os.environ['TCPFIT_TEST_FIXTURE'])
url=next(arg for arg in sys.argv[1:] if arg.startswith('https://'))
if 'api.github.com' in url:
    data=b'{"tag_name":"v0.6.1"}'
else:
    name=url.rsplit('/',1)[-1]
    data=(root/name).read_bytes()
    if name=='tcpfit-client.ps1' and os.environ.get('TCPFIT_TEST_DAMAGE')=='1':
        data+=b'\\n# test-only-corruption\\n'
if '-o' in sys.argv:
    pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_bytes(data)
else:
    sys.stdout.buffer.write(data)
''', encoding="utf-8")
            fake_curl.chmod(0o700)
            env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"], TCPFIT_TEST_FIXTURE=str(fixture), TCPFIT_NO_TELEMETRY="1")
            def publish(version):
                hashes = []
                for name in ("tcpfit.sh", "install.sh", "tcpfit-return.py", "tcpfit-client.sh", "tcpfit-client.ps1"):
                    content = (ROOT / name).read_text(encoding="utf-8").replace(SOURCE_VERSION, version).encode("utf-8")
                    (fixture / name).write_bytes(content)
                    hashes.append(hashlib.sha256(content).hexdigest() + "  " + name)
                (fixture / "SHA256SUMS").write_text("\n".join(hashes) + "\n", encoding="utf-8")
            def run(*args, damage=False):
                return subprocess.run(args, env=dict(env, TCPFIT_TEST_DAMAGE="1" if damage else "0"), text=True, capture_output=True, timeout=30)
            publish("0.6.0")
            installed = run("bash", str(ROOT / "install.sh"))
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            entry = Path("/usr/local/bin/tcpfit")
            for name in ("tcpfit.sh", "tcpfit-return.py", "tcpfit-client.sh", "tcpfit-client.ps1"):
                self.assertEqual((Path("/usr/local/lib/tcpfit/0.6.0") / name).read_bytes(), (fixture / name).read_bytes())
            resolved = run("bash", "-c", 'source "$1"; return_assets; test "$RETURN_HELPER" = /usr/local/lib/tcpfit/0.6.0/tcpfit-return.py', "test-assets", str(entry))
            self.assertEqual(resolved.returncode, 0, resolved.stdout + resolved.stderr)
            original = entry.read_bytes()
            publish("0.6.1")
            update = 'source "$1"; confirm(){ return 0; }; cmd_update'
            failed = run("bash", "-c", update, "test-update", str(entry), damage=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("校验失败", failed.stdout + failed.stderr)
            self.assertEqual(entry.read_bytes(), original)
            passed = run("bash", "-c", update, "test-update", str(entry))
            self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
            self.assertEqual(entry.read_bytes(), (fixture / "tcpfit.sh").read_bytes())
            for name in ("tcpfit.sh", "tcpfit-return.py", "tcpfit-client.sh", "tcpfit-client.ps1"):
                self.assertEqual((Path("/usr/local/lib/tcpfit/0.6.1") / name).read_bytes(), (fixture / name).read_bytes())


if __name__ == "__main__":
    unittest.main()
