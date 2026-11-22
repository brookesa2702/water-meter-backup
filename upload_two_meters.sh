#!/bin/bash
set -uo pipefail
cd /home/pi/water-meter
failed=0
for meter in house garden; do
    rclone copyto "${meter}_readings.csv" "gdrive:${meter}_readings.csv" --retries 3 --contimeout 30s --timeout 60s -v || failed=1
done
exit "$failed"
