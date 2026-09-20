#!/usr/bin/env bash
# Block until the driver exits, then print the log.
for i in $(seq 1 240); do
  [ -f /tmp/lens-harvest/run-exit.txt ] && break
  sleep 10
done
echo "=== run-exit ==="
cat /tmp/lens-harvest/run-exit.txt 2>&1
echo "=== outer log ==="
cat /tmp/lens-harvest/run-outer.log 2>&1
