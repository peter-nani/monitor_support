#!/bin/bash

LOG="/home/hr306/DockerSetups/monitor_support/monitor_support/monitor_support.log"

cd /home/hr306/DockerSetups/monitor_support/monitor_support || exit 1

echo "$(date '+%Y-%m-%d %H:%M:%S') - monitor_support scheduler started" >> "$LOG"

while true
do
    START=$(date +%s)

    echo "$(date '+%Y-%m-%d %H:%M:%S') - Starting monitor_support" >> "$LOG"

    docker compose run --rm monitor_support >> "$LOG" 2>&1

    END=$(date +%s)

    ELAPSED=$((END - START))
    SLEEP_TIME=$((2700 - ELAPSED))

    echo "$(date '+%Y-%m-%d %H:%M:%S') - Job finished in ${ELAPSED}s" >> "$LOG"

    if [ "$SLEEP_TIME" -gt 0 ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') - Sleeping ${SLEEP_TIME}s until next run" >> "$LOG"
        sleep "$SLEEP_TIME"
    fi
done
