#!/usr/bin/env bash

set -euo pipefail

declare -r pipeline_file=${1:?missing pipeline file}

echo "Adding sandbox-check parameter to the init task"
pmt modify -f "$pipeline_file" task init add-param sandbox-check enabled
