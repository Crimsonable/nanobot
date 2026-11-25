#!/usr/bin/env bash
# Connected Linux x86_64 build host: Python 3.12, npm, curl.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p vendor/wheels
bash scripts/vendor_bubblewrap.sh
curl --fail --location --output vendor/opencode.tar.gz \
  https://github.com/anomalyco/opencode/releases/download/v2.3.2/opencode-linux-x64.tar.gz
echo '563b5d663637226b6e3bb4bb6803281bc39b0d2cd88db06d674b6420aab009b0  vendor/opencode.tar.gz' | sha256sum -c -
tar xzf vendor/opencode.tar.gz -C vendor
(cd vendor && sha256sum opencode > opencode.sha256)
(cd vendor && sha256sum -c opencode.sha256)
chmod 0555 vendor/opencode
python3 -m pip download --require-hashes --only-binary=:all: -r requirements-backend.lock -d vendor/wheels
python3 -m pip download --require-hashes --only-binary=:all: -r requirements-toolchain.lock -d vendor/wheels
(
  cd opencode_plugin
  npm ci
  npm run build
)
echo 'Dependencies ready for base images. Install vendor/opencode into the shared server opencode directory.'
echo 'Copy source including opencode_plugin/package.json, opencode_plugin/dist and opencode_plugin/snapshot.py into the server source directory.'
