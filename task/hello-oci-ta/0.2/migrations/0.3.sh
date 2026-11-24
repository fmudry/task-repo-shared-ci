#!/usr/bin/env bash

set -euo pipefail

declare -r pipeline_file=${1:?missing pipeline file}

pipeline_name=$(yq ".metadata.name" /dev/stdin <"$pipeline_file")
echo "Adding sandbox-check parameter to the init task of $pipeline_name"
pmt modify -f "$pipeline_file" task init add-param sandbox-check enabled
