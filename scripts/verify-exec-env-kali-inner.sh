#!/usr/bin/env bash
# In-container functional checks for the CTF exec image. Invoked by
# scripts/verify-exec-env-kali.sh; every check runs the tool on real input.
set -u
fail=0
ok()  { printf 'PASS  %-26s %s\n' "$1" "${2:-}"; }
bad() { printf 'FAIL  %-26s %s\n' "$1" "${2:-}"; fail=$((fail+1)); }

check(){ # check <name> <command...>
  local name="$1"; shift
  local out
  if out=$("$@" 2>&1); then ok "$name" "$(printf '%s' "$out" | head -1 | cut -c1-90)"
  else bad "$name" "$(printf '%s' "$out" | tail -1 | cut -c1-90)"; fi
}
grep_check(){ # grep_check <name> <pattern> <command...>
  local name="$1" pattern="$2"; shift 2
  local out
  out=$("$@" 2>&1)
  if printf '%s' "$out" | grep -qE "$pattern"; then
    ok "$name" "$(printf '%s' "$out" | grep -oE "$pattern" | head -1)"
  else bad "$name" "$(printf '%s' "$out" | tail -1 | cut -c1-90)"; fi
}

### 1. envd control plane and interpreter separation -------------------------
check "envd package"             /opt/envd/venv/bin/python -c "import bbx_envd, uvicorn, sys; print(sys.version.split()[0])"
check "traffic venv (mitmproxy)" /opt/traffic/venv/bin/mitmdump --version
check "system python 3.14"       python3 -c "import sys; print(sys.version.split()[0])"
check "envd python 3.13"         python3.13 -c "import sys; print(sys.version.split()[0])"
check "agent group + sudoers"    bash -c "getent group bbx-agents >/dev/null && visudo -cf /etc/sudoers >/dev/null && echo bbX-groups-ok"
check "runuser"                  runuser --version

### 2. archives --------------------------------------------------------------
check "7z roundtrip"             bash -c "cd /tmp && printf hello > a.txt && 7z a -bso0 -bsp0 a.7z a.txt >/dev/null && 7z x -bso0 -bsp0 -oout a.7z >/dev/null && cmp a.txt out/a.txt && echo roundtrip-ok"
check "zip/unzip roundtrip"      bash -c "cd /tmp && zip -q b.zip a.txt && unzip -oq b.zip -d outzip && cmp a.txt outzip/a.txt && echo roundtrip-ok"
check "unrar present"            bash -c "unrar 2>&1 | head -1"
check "cabextract present"       cabextract -v
check "ncompress present"        bash -c "compress -V 2>&1 | head -1"

### 3. runtimes --------------------------------------------------------------
check "node"                     node -e "console.log(process.version)"
check "npm"                      npm --version

### 4. web fuzzing -----------------------------------------------------------
check "ffuf live fuzz"           bash -c "cd /tmp && (python3 -m http.server 8137 >/dev/null 2>&1 & echo \$! > /tmp/srv.pid); sleep 1.5; printf 'index.html\n' > /tmp/wl.txt; ffuf -s -u http://127.0.0.1:8137/FUZZ -w /tmp/wl.txt -mc 200 2>&1 | head -1; kill \$(cat /tmp/srv.pid)"
check "gobuster present"         gobuster --version

### 5. reverse engineering ---------------------------------------------------
check "radare2 analysis"         bash -c "printf '#include <stdio.h>\nint main(){puts(\"hi\");return 0;}\n' > /tmp/h.c && gcc -o /tmp/h /tmp/h.c && r2 -q -c 'aaa; afl~main' /tmp/h | head -2"
check "pwndbg version"           pwndbg --version
check "pwndbg inside gdb"        bash -c "pwndbg -q -batch -ex 'pi import pwndbg; print(pwndbg.__version__)' 2>&1 | tail -1"
check "gef loads in stock gdb"   bash -c "gdb -q -batch -ex 'source /usr/share/gdb/gef.py' -ex 'gef help' /bin/true 2>&1 | grep -qi gef && echo gef-loaded"
check "checksec"                 bash -c "checksec --file=/bin/true 2>&1 | head -2"

### 6. crypto ----------------------------------------------------------------
grep_check "sage: factor"        '\-?[0-9]+ \* [0-9]+' sage -c 'print(factor(2^64 - 1))'
grep_check "sage: discrete_log"  '^3$' sage -c 'print(discrete_log(Mod(6,7), Mod(3,7)))'
grep_check "sage: LLL"           'reduced' sage -c 'M = Matrix(ZZ, [[1,2],[3,4]]); print("reduced", M.LLL().rows())'
grep_check "sage: GF poly"       'x \+ 1' sage -c 'R.<x> = GF(2)[]; print((x^2 + 1) // (x + 1))'
check "hashcat crack md5"        bash -c "hashcat -m 0 -a 3 --potfile-disable --force 900150983cd24fb0d6963f7d28e17f72 ?l?l?l 2>/dev/null | grep -q Cracked && echo abc-cracked"
check "john crack raw-md5"       bash -c "printf 'password\n' > /tmp/wl && printf 'u:5f4dcc3b5aa765d61d8327deb882cf99\n' > /tmp/h && john --wordlist=/tmp/wl --format=raw-md5 --pot=/tmp/j.pot /tmp/h >/dev/null 2>&1; john --show --format=raw-md5 --pot=/tmp/j.pot /tmp/h 2>/dev/null | grep -q password && echo password-cracked"
check "python crypto stack"      python3 -c "import pwn, z3, Crypto, gmpy2, sympy, elftools, requests; print('crypto-stack-ok')"
check "angr project load"        python3 -c "import angr; p = angr.Project('/bin/true', auto_load_libs=False); print('arch', p.arch.name)"
check "fpylll reduction"         python3 -c "from fpylll import IntegerMatrix, LLL; A = IntegerMatrix.random(4, 'uniform', bits=20); B = LLL.reduction(A); print('lll', B.nrows, 'x', B.ncols)"

### 7. forensics -------------------------------------------------------------
check "Pillow"                   bash -c "python3 -c \"from PIL import Image; Image.new('RGB',(3,3)).save('/tmp/p.png'); print('PIL ok')\""
check "exiftool"                 bash -c "python3 -c \"from PIL import Image; Image.new('RGB',(3,3)).save('/tmp/p.jpg')\" && exiftool -s -FileType /tmp/p.jpg"
check "pngcheck"                 bash -c "pngcheck -q /tmp/p.png && echo png-ok"
check "imagemagick"              bash -c "convert -size 4x4 xc:red /tmp/i.jpg && identify -format '%m %wx%h' /tmp/i.jpg"
check "steghide"                 bash -c "steghide --version 2>&1 | head -1"
check "foremost"                 bash -c "foremost -V 2>&1 | head -1"
check "sleuthkit fls"            bash -c "fls -V 2>&1 | head -1"
check "testdisk"                 bash -c "testdisk /version 2>&1 | head -1"
check "oletools"                 bash -c "olevba --help 2>&1 | head -1"

### 8. mobile -----------------------------------------------------------------
check "apktool"                  bash -c "apktool --version 2>&1 | head -1"
check "jadx"                     bash -c "jadx --version 2>&1 | head -1"
check "dex2jar present"          bash -c "ls -d /usr/share/dex2jar >/dev/null && echo present"

### 9. network clients ----------------------------------------------------------
check "openssh client"           bash -c "ssh -V 2>&1 | head -1"
check "dig + resolution"         bash -c "dig -v 2>&1 | head -1; getent hosts localhost"
check "ss + ping loopback"       bash -c "ss -ltn >/dev/null && ping -c1 -W2 127.0.0.1 >/dev/null && echo loopback-ok"
check "traceroute"               bash -c "traceroute --version 2>&1 | head -1"
check "whois"                    bash -c "whois --version 2>&1 | head -1"
check "impacket"                 python3 -c "import impacket; print('impacket ok')"
check "php-cli"                  bash -c "php -r 'echo PHP_VERSION;'"

### 10. pwn helpers --------------------------------------------------------------
check "seccomp-tools"            bash -c "seccomp-tools --version 2>&1 | head -1"
check "one_gadget"               bash -c "one_gadget --version 2>&1 | head -1"

### 11. browser automation stays out ---------------------------------------------
check "no browser automation"    bash -c "for b in chromium chromium-browser google-chrome playwright; do command -v \$b >/dev/null && exit 1; done; python3 -c 'import playwright' 2>/dev/null && exit 1; echo confirmed-absent"
check "hashcat opencl device"    bash -c "clinfo -l 2>/dev/null | grep -qi 'Device #0' && echo opencl-device-present"

echo
if [ "$fail" -eq 0 ]; then echo "ALL CTF TOOLCHAIN CHECKS PASSED"
else echo "$fail CHECK(S) FAILED"; fi
exit "$fail"
