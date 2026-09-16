#!/bin/bash
set -euo pipefail

# Pinned from the official baidu-netdisk/bdpan-storage repository.
SKILL_COMMIT="069dc7ee9f51e5a4595abd221d6263f000eaa340"
SCRIPT_SHA256="4e8ecb967a12ba7a0648ffcaca1cb05796e4bf824b884b724e88784626bacc10"
SCRIPT_URL="https://raw.githubusercontent.com/baidu-netdisk/bdpan-storage/${SKILL_COMMIT}/skills/baidu-drive/scripts/install.sh"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

curl \
  --fail \
  --silent \
  --show-error \
  --location \
  --retry 5 \
  --retry-all-errors \
  --connect-timeout 20 \
  --max-time 180 \
  "${SCRIPT_URL}" \
  --output "${WORK_DIR}/install.sh"
printf '%s  %s\n' "${SCRIPT_SHA256}" "${WORK_DIR}/install.sh" | sha256sum --check --status

cd "${WORK_DIR}"
bash ./install.sh "$@"
