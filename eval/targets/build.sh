#!/bin/sh
set -eu
# Keep macOS resource-fork metadata out of portable task inputs.
export COPYFILE_DISABLE=1
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
mkdir -p "$root/dist"
tmp=$(mktemp -d "${TMPDIR:-/tmp}/bbx-eval-build.XXXXXX")
trap 'rm -rf "$tmp"' EXIT HUP INT TERM

mkdir "$tmp/order-service"
cp -R "$root/order-service/." "$tmp/order-service/"
find "$tmp/order-service" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$tmp/order-service" -name .pytest_cache -type d -prune -exec rm -rf {} +
tar -C "$tmp/order-service" -czf "$root/dist/order-service.tar.gz" .
tar -C "$root/attendance-reconciliation" -czf "$root/dist/attendance-reconciliation.tar.gz" .

mkdir "$tmp/mini-shop"
cp -R "$root/mini-shop/src-main/." "$tmp/mini-shop/"
cd "$tmp/mini-shop"
find . -name __pycache__ -type d -prune -exec rm -rf {} +
find . -name .pytest_cache -type d -prune -exec rm -rf {} +
git init -q -b main
git -c user.name='Mini Shop Team' -c user.email='team@example.invalid' add .
git -c user.name='Mini Shop Team' -c user.email='team@example.invalid' commit -qm 'Initial shop service'
git switch -qc feature/coupon-refund
for layer in "$root"/mini-shop/commits/*; do
    if [ -d "$layer/shop" ]; then cp -R "$layer/shop/." shop/; fi
    if [ -d "$layer/tests" ]; then cp -R "$layer/tests/." tests/; fi
    if [ -s "$layer/app_append.txt" ]; then
        printf '\n' >> shop/app.py
        cat "$layer/app_append.txt" >> shop/app.py
    fi
    if [ -s "$layer/seed_append.txt" ]; then
        printf '\n' >> scripts/seed.py
        cat "$layer/seed_append.txt" >> scripts/seed.py
    fi
    find . -name __pycache__ -type d -prune -exec rm -rf {} +
    find . -name .pytest_cache -type d -prune -exec rm -rf {} +
    git -c user.name='Mini Shop Team' -c user.email='team@example.invalid' add .
    git -c user.name='Mini Shop Team' -c user.email='team@example.invalid' commit -qm "$(cat "$layer/message.txt")"
done
find .git/hooks -name "*.sample" -type f -delete
tar -C "$tmp/mini-shop" -czf "$root/dist/mini-shop.tar.gz" .
printf 'Built %s, %s and %s\n' "$root/dist/order-service.tar.gz" "$root/dist/mini-shop.tar.gz" "$root/dist/attendance-reconciliation.tar.gz"
