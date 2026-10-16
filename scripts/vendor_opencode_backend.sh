#!/usr/bin/env bash
# Connected Linux x86_64 build host: Python 3.12, npm, curl.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p vendor/wheels
bash scripts/vendor_bubblewrap.sh
curl --fail --location --output vendor/opencode.tar.gz \
  https://github.com/anomalyco/opencode/releases/download/v1.18.31/opencode-linux-x64.tar.gz
echo 'e9312be75ed803b7415fc2aeabda1f4fe938912a39673762dc0c38c0e11ebde4  vendor/opencode.tar.gz' | sha256sum -c -
tar xzf vendor/opencode.tar.gz -C vendor
echo 'f9dab32248695e9ebd56b16a1921798fd85112cf5a69c7dfd0cabc1e17be4a11  opencode' > vendor/opencode.sha256
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
echo 'Copy source including opencode_plugin/dist, package files and node_modules into the server source directory.'
