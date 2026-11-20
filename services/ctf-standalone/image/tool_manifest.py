"""Record the installed command paths and package versions without network access."""

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
from pathlib import Path

COMMANDS = """bash python3 node npm ruby php java javac jar gcc g++ make cmake
pkg-config nasm gdb pwndbg gef checksec pwn ROPgadget one_gadget
seccomp-tools patchelf r2 jadx sage sage-python z3 openssl hashcat john
clinfo exiftool binwalk foremost steghide pngcheck magick tshark tcpdump olevba
mmls testdisk 7z unrar curl wget nc socat ssh dig ffuf gobuster nmap sqlmap
dirsearch sox ffmpeg ffprobe multimon-ng zbarimg qrencode xxd qpdf pdfinfo
pdftotext bkcrack zsteg vol""".split()

CAPABILITY_CHECKS = {
    "pwn": ["gcc_compile", "gdb_run", "pwndbg", "gef", "pwntools", "ropgadget"],
    "reverse": ["ghidra_headless", "radare2_analysis", "jadx_validate", "angr_symbolic_step"],
    "crypto": [
        "sage_factor_lll",
        "sage_scientific",
        "sage_cython_math",
        "gap_terminal_help",
        "maxima_terminal_help",
        "z3",
        "crypto",
    ],
    "web": [
        "web_python",
        "nmap_local",
        "sqlmap_local",
        "dirsearch_version",
        "ffuf_validate",
        "gobuster_local",
    ],
    "pcap": ["scapy", "tshark_offline"],
    "image_qr": ["opencv", "qr_read", "pngcheck", "exiftool", "imagemagick", "zsteg"],
    "audio": ["audio_convert", "audio_probe", "multimon_dtmf"],
    "archive": ["archive_validate", "bkcrack_decrypt", "bkcrack_validate"],
    "pdf": ["pdf_info", "pdf_text", "pdf_structure"],
    "password": ["john", "hashcat_cpu_device"],
    "node_sqlite": ["node_sqlite"],
}
CAPABILITY_SCOPE = {
    "pwn": "Compile/run a local ELF, debug it, import helpers, and find simple ROP gadgets",
    "reverse": "Ghidra ELF analysis, JADX Java decompilation, radare2, and one angr symbolic step",
    "crypto": "Sage factorization/LLL, NumPy/SciPy, small Z3 and crypto-library fixtures",
    "web": "Local ffuf/gobuster, nmap TCP-connect scan and sqlmap SQLite boolean detection; "
    "dirsearch version only",
    "pcap": "Generate and parse a synthetic offline PCAP; no live capture required",
    "image_qr": "Local PNG/QR generation, decoding, conversion and zsteg invocation",
    "audio": "Local tone/FLAC roundtrip and synthetic DTMF decoding",
    "archive": "7z roundtrip and bkcrack ZipCrypto decryption with known local fixture keys",
    "pdf": "Parse and extract text from a small local PDF and validate its structure",
    "password": "John self-test and hashcat CPU OpenCL enumeration; no speed benchmark",
    "node_sqlite": "An in-memory node:sqlite query",
}


def python_modules(executable: str) -> dict:
    names = (
        "pwn z3 Crypto gmpy2 sympy fpylll angr claripy keystone cv2 scapy numpy scipy "
        "httpx bs4 lxml volatility3"
    ).split()
    code = (
        "import importlib.util,importlib.metadata as m,json; "
        f"names={names!r}; packages=m.packages_distributions(); "
        "print(json.dumps({n:{'present':importlib.util.find_spec(n) is not None,"
        "'packages':{p:m.version(p) for p in packages.get(n,[])}} for n in names}))"
    )
    return json.loads(subprocess.check_output([executable, "-c", code], text=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--smoke", type=Path)
    args = parser.parse_args()
    package_list = subprocess.check_output(
        ["dpkg-query", "-W", "-f=${Package}\t${Version}\t${db:Status-Status}\n"], text=True
    )
    packages = {}
    for row in package_list.splitlines():
        name, version, status = row.split("\t", 2)
        if status == "installed":
            packages[name] = version
    versions = {}
    for name, command in {
        "python": ["python3", "--version"],
        "sage_python": ["sage-python", "--version"],
        "node": ["node", "--version"],
        "npm": ["npm", "--version"],
        "sage": ["sage", "--version"],
    }.items():
        versions[name] = subprocess.check_output(
            command, text=True, stderr=subprocess.STDOUT
        ).strip()
    smoke = None
    capabilities = {}
    if args.smoke:
        reports = [
            json.loads(row)["final"]
            for row in args.smoke.read_text().splitlines()
            if row.startswith('{"final":')
        ]
        assert len(reports) == 1 and reports[0]["passed"], "Invalid smoke verification"
        smoke = reports[0]
        assert smoke["uid"] == 10000
    checks = {result["name"]: result for result in smoke["results"]} if smoke else {}
    for name, required in CAPABILITY_CHECKS.items():
        passed = all(checks.get(check, {}).get("passed", False) for check in required)
        capabilities[name] = {
            "status": "available" if passed else "conditional",
            "verified_checks": required if passed else [],
            "scope": CAPABILITY_SCOPE[name] if passed else "not yet functionally verified",
        }
    capabilities["gpu"] = {"status": "unavailable", "scope": "No GPU device tested or required"}
    capabilities["memory_forensics_symbols"] = {
        "status": "conditional",
        "scope": "Volatility3 CLI/module smoke only; target-specific symbols may be needed",
    }
    capabilities["competition_harness"] = {
        "status": "unavailable",
        "scope": "Tools image only; no Lead/Worker program",
    }
    capabilities["opencv_gui"] = {"status": "unavailable", "scope": "Headless OpenCV build"}
    capabilities["java_runtime_linker"] = {
        "status": "unavailable",
        "scope": "JDK jmods omitted; java/javac/jar remain",
    }
    capabilities["optional_ghidra_extensions"] = {
        "status": "unavailable",
        "scope": "Uninstalled extension ZIPs omitted; core preserved",
    }
    capabilities["foreign_architecture_execution"] = {
        "status": "unavailable",
        "scope": "QEMU user and multiarch GDB omitted; native debugging remains",
    }
    capabilities["apk_repackaging"] = {
        "status": "unavailable",
        "scope": "APKTool/dex2jar omitted; Java and JADX inspection remain",
    }
    capabilities["sage_browser_notebooks"] = {
        "status": "unavailable",
        "scope": "Browser assets and Pandoc omitted; CLI/IPython/Cython remain",
    }
    manifest = {
        "schema_version": 1,
        "purpose": "tools-base-only",
        "architecture": platform.machine(),
        "path": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "commands": {name: shutil.which(name) for name in COMMANDS},
        "special_paths": {
            "sage_python": "/usr/local/bin/sage-python",
            "sage_python_interpreter": "/opt/sage/bin/python",
            "ghidra_headless": "/usr/share/ghidra/support/analyzeHeadless",
            "john_dictionary": "/usr/share/john/password.lst",
        },
        "versions": versions,
        "debian_packages": packages,
        "ruby_gems": subprocess.check_output(["gem", "list", "--local"], text=True).splitlines(),
        "extra_sources": json.loads(Path("/opt/ctf-tools/sources.json").read_text()),
        "wordlists": json.loads(Path("/opt/ctf-tools/wordlists.json").read_text()),
        "python_modules": {
            "/usr/bin/python3": python_modules("/usr/bin/python3"),
            "/opt/sage/bin/python": python_modules("/opt/sage/bin/python"),
        },
        "capabilities": capabilities,
        "verification": smoke,
        "usage_notes": [
            "Use absolute paths for JADX input/output.",
            "Ghidra project paths must not contain hidden dot-prefixed components.",
            "Use sage-python for the scientific environment and matching Cython build tools.",
            "DEBUGINFOD_URLS is empty; optional debugger downloads are disabled.",
            "No competition Agent harness or challenge answers are included.",
        ],
    }
    destination = args.destination
    destination.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    destination.with_suffix(".sha256").write_text(
        hashlib.sha256(destination.read_bytes()).hexdigest() + "  " + destination.name + "\n"
    )
    destination.with_name("worker-tools.md").write_text(
        "本执行容器提供以下工具；完整版本、模块路由和验证范围见 /opt/ctf-tools/manifest.json。\n"
        "Pwn：gcc/g++、gdb、pwndbg/GEF、pwntools、ROPgadget、"
        "checksec、one_gadget、seccomp-tools、patchelf。\n"
        "逆向：radare2、Ghidra headless、angr、JADX；保留 Java 编译与运行。"
        "本精简镜像没有 QEMU、多架构 GDB 或 APK 重打包链。\n"
        "密码数学：Sage、Z3、PyCryptodome、gmpy2、sympy、fpylll。"
        "科学计算使用 sage-python（解释器 /opt/sage/bin/python，含 NumPy/SciPy/Cython 环境）。\n"
        "网络/Web：curl/wget、nc/socat、SSH/DNS、nmap、sqlmap、dirsearch、ffuf/gobuster、"
        "requests/httpx/bs4。普通用户扫描使用 nmap -sT；原始报文权限由比赛环境决定。\n"
        "文件/Misc：tshark/Scapy 离线 PCAP、ExifTool、OpenCV/PIL、ImageMagick、"
        "binwalk/foremost/steghide/zsteg、ffmpeg/sox/multimon-ng、zbarimg/qrencode、"
        "7z/unrar/bkcrack、John、hashcat CPU OpenCL、Poppler/qpdf、Volatility3（符号可能需补）。\n"
        "Ghidra：/usr/share/ghidra/support/analyzeHeadless；项目路径不要使用隐藏目录。"
        "JADX 输入输出使用绝对路径。John 可直接用 john，内置字典在"
        " /usr/share/john/password.lst；/usr/share/seclists 是6份常用 Web/用户名词典的精选子集，"
        "不是完整 SecLists 或 rockyou。\n"
        f"默认 {versions['python']}；Node {versions['node']}；{versions['sage']}。"
        "工具说明不代表任意旧版本脚本兼容，先按题目选工具，不要逐一探测全部工具。\n"
    )


if __name__ == "__main__":
    main()
