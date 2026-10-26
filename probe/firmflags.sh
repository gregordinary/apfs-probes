#!/usr/bin/env bash
# How macOS keeps its own firmlinks' records, read from the runner's system
# volume through its raw device.
#
#   system   firmflagsutil.py system, as root: the system volume's
#            file-system tree walked from the container's raw device, and,
#            of each record of the attribute com.apple.fs.firmlink and of
#            the inode that carries it, the flags it holds, as counts and
#            flag values alone (log/firmflags.json)
#
# Usage: firmflags.sh OUTDIR

. "$(dirname "$0")/lib.sh" "$1"

section env
step sw_vers 30 sw_vers
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'

section system
sstep firmflags 1200 "$PY" "$HERE/firmflagsutil.py" system "$OUT/log/firmflags.json"

section done
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
exit 0
