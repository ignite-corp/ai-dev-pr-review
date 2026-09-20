#!/usr/bin/env bash
# Is the missing incremental output my awk|tee pipeline, or the producer?
python3 -u -c "
import sys,time
for i in range(3):
    print('line', i, time.time(), flush=True)
    time.sleep(2)
" 2>&1 | awk '{printf "%s %s\n", strftime("%H:%M:%S"), $0; fflush()}' | tee /tmp/lens-harvest/buftest.log
echo "--- file contents ---"
cat /tmp/lens-harvest/buftest.log
awk --version 2>&1 | head -1
