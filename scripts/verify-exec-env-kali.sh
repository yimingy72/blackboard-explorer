#!/usr/bin/env bash
# Drive scripts/verify-exec-env-kali-inner.sh inside the CTF exec image.
# Usage: scripts/verify-exec-env-kali.sh [image]   (default bbx-exec-env-kali:latest)
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
image="${1:-bbx-exec-env-kali:latest}"
echo "# verifying $image inside a throwaway container"
docker run --rm -v "$here/verify-exec-env-kali-inner.sh:/usr/local/bin/verify-ctf-tools:ro" \
  "$image" bash /usr/local/bin/verify-ctf-tools
