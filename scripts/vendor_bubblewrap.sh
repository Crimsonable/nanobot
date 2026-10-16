#!/usr/bin/env bash
# Debian 13 amd64 package before the pre-5.10 kernel fallback was removed.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p vendor
package=vendor/bubblewrap_0.11.0-2+deb13u1_amd64.deb
checksum=7a541ad9d12b23db7082ed3bf1f6d5253f23439cdfa7c28b4e022d0a602a19d6
if ! echo "$checksum  $package" | sha256sum -c - >/dev/null 2>&1; then
  curl --fail --location --retry 3 --connect-timeout 20 --max-time 180 \
    https://snapshot.debian.org/file/817a2e6186906394f63696c2b289f0f19e45b8d3 \
    --output "$package.new"
  echo "$checksum  $package.new" | sha256sum -c -
  mv -f "$package.new" "$package"
fi
echo "$checksum  $package" | sha256sum -c -
