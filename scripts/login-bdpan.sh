#!/bin/bash
set -euo pipefail

# Login remains an explicit operation and follows the official OOB login script.
SKILL_COMMIT="069dc7ee9f51e5a4595abd221d6263f000eaa340"
SCRIPT_SHA256="624b26ff330a8d0caace954af55f6b23ba2c665e98f5bd31da370458858e8030"
SCRIPT_URL="https://raw.githubusercontent.com/baidu-netdisk/bdpan-storage/${SKILL_COMMIT}/skills/baidu-drive/scripts/login.sh"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

curl --fail --silent --show-error --location "${SCRIPT_URL}" --output "${WORK_DIR}/login.sh"
printf '%s  %s\n' "${SCRIPT_SHA256}" "${WORK_DIR}/login.sh" | sha256sum --check --status

bash "${WORK_DIR}/login.sh" "$@"
