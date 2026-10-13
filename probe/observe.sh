#!/usr/bin/env bash
# What a runner lets a probe observe of APFS besides the images it leaves:
# the blocks one operation writes, under three tracers; the unified log at its
# debug level; the kernel's sysctl names and registry properties; the verbose
# modes of the tools in apfs.fs; Endpoint Security; and whether a virtual
# machine starts inside the runner.
#
#   env       versions, hardware, privileges and System Integrity Protection,
#             and the manual pages of the tracers and of the calls made here
#   knobs     the sysctl names that mention APFS
#   op        the operation every tracer watches. A copy of a fresh 64 MiB
#             image is attached, its volume mounted, sixteen files of 68 KiB,
#             an uncommon length for one I/O, written and synced, and each file's extents asked of the kernel
#             with fcntl F_LOG2PHYS_EXT (l2p.c); then the volume is unmounted,
#             the image detached, and every extent compared with the image at
#             its device offset (verify-NAME.json). On a container, apfs_stats
#             is sampled once mounted and again after the write.
#   fs_usage  the operation under fs_usage -w -f diskio
#   dtrace    whether DTrace starts and lists an io provider, and the
#             operation under io:::start
#   ktrace    the event codes that carry disk I/O, found by value. The
#             operation runs on an HFS+ volume under ktrace trace -f ALL, and
#             the codes whose events start at the files' extents are kept
#             (ktrace-control.json, ktrace-control.ndjson), with what ktrace
#             decode says of each; no other event is kept. The operation then
#             runs on a container under a filter of those codes' subclasses
#             (ktrace-apfs.ndjson), so every event chosen is one a volume of
#             another format emits as well.
#   log       the unified log with the APFS subsystem at debug level, around a
#             format, a check and the operation
#   tools     the registry properties of an attached container and its
#             volume, apfs_checkseal -d, apfs_stats -e and sm_stats -v
#   vm        whether Hypervisor.framework creates a virtual machine
#             (hvcreate.c), and what Virtualization.framework reports
#             (vzsupport.swift)
#   es        eslogger's file events around the operation, and the templates
#             xctrace lists
#
# observeutil.py reads each trace against the files' extents: fs_usage's block
# numbers on the volume's disk and the container's in fsu-match.json and
# fsu-match-container.json, DTrace's, whose device has no name there, in
# dtrace-match.json, and ktrace's events in ktrace-control.json and
# ktrace-apfs.json.
#
# Usage: observe.sh OUTDIR

. "$(dirname "$0")/lib.sh" "$1"
OBS="$HERE/observeutil.py"

# tracer NAME LIMIT STDOUT CMD... : start CMD as root in the background, its
# output in STDOUT and its errors in log/NAME.err, under a limit of LIMIT
# seconds, and give it five seconds to start. Fails if it has exited by then.
# tracer_stop ends it.
tracer() {
    local name=$1 limit=$2 stdout=$3 st
    shift 3
    T_NAME=$name T_CMD="$*" T_START=$(date +%s)
    sudo -n perl -e 'alarm shift; exec { $ARGV[0] } @ARGV or die "exec: $!\n"' "$limit" "$@" \
        >"$stdout" 2>"$OUT/log/$name.err" </dev/null &
    T_PID=$!
    sleep 5
    st=$(ps -o stat= -p "$T_PID" 2>/dev/null | tr -d ' ')
    case $st in
        "" | Z*) return 1 ;;
    esac
}

# tracer_stop PROCESS : three seconds on, interrupt the tracer, whose process
# is named PROCESS, give it ten seconds to finish, then end it, wait for it,
# and record it as a step.
tracer_stop() {
    local rc i st
    sleep 3
    sudo -n pkill -INT -x "$1"
    for i in 1 2 3 4 5 6 7 8 9 10; do
        st=$(ps -o stat= -p "$T_PID" 2>/dev/null | tr -d ' ')
        case $st in "" | Z*) break ;; esac
        sleep 1
    done
    case $st in "" | Z*) ;; *) sudo -n pkill -TERM -x "$1" ;; esac
    wait "$T_PID"
    rc=$?
    [ -s "$OUT/log/$T_NAME.err" ] || rm -f "$OUT/log/$T_NAME.err"
    _record "$T_NAME" "$rc" $(($(date +%s) - T_START)) "sudo $T_CMD"
}

# op NAME BASE KIND : the operation, on a fresh copy of BASE, an image holding
# a container (KIND apfs) or an HFS+ volume (KIND hfs) on the whole disk.
# log/NAME-l2p.out holds the files' extents. op-NAME.disk names the attached
# disk, and op-NAME.vol the disk mounted, which on a container is the volume:
# the kernel reports a file's data I/O on the volume's disk and the
# container's own on the container's.
op() {
    local n=$1 base=$2 kind=$3 img="$WORK/op-$1.img" mnt="$WORK/mnt-$1" dev cont vol
    step "$n-copy-image" 60 cp "$base" "$img" || return 1
    dev=$(attach_raw "$n" "$img") || return 1
    echo "${dev#/dev/}" >"$OUT/op-$n.disk"
    vol=$dev
    if [ "$kind" = apfs ]; then
        if cont=$(container_of "$n-list" "$dev") && vol=$(first_volume "$n-list" "$cont"); then
            vol=/dev/$vol
            echo "$cont" >"$OUT/op-$n.cont"
        else
            detach "$n" "$dev"
            return 1
        fi
    fi
    echo "${vol#/dev/}" >"$OUT/op-$n.vol"
    if mount_vol "$n" "$vol" "$mnt"; then
        [ "$kind" = apfs ] && sstep "$n-stats-mounted" 30 "$RES/apfs_stats"
        sstep "$n-write" 120 sh -c 'for i in $(seq -w 0 15); do dd if=/dev/urandom of="$1/f$i" bs=4096 count=17 2>/dev/null || exit 1; done' sh "$mnt"
        sstep "$n-sync" 60 sync
        [ "$kind" = apfs ] && sstep "$n-stats-written" 30 "$RES/apfs_stats"
        sstep "$n-l2p" 30 sh -c '"$1" "$2"/f*' sh "$WORK/l2p" "$mnt"
        sstep "$n-files" 60 sh -c 'mkdir -p "$2" && cp "$1"/f* "$2"/ && chmod -R a+rX "$2"' \
            sh "$mnt" "$WORK/files-$n"
        sstep "$n-ls" 30 ls -lai "$mnt"
        unmount_vol "$n" "$vol"
    fi
    detach "$n" "$dev"
    [ -s "$OUT/log/$n-l2p.out" ] &&
        step "$n-verify" 120 "$PY" "$OBS" verify "$img" "$OUT/log/$n-l2p.out" "$WORK/files-$n" \
            "$OUT/verify-$n.json"
    rm -f "$img"
}

section env
step sw_vers 30 sw_vers
step uname 30 uname -a
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'
step sysctl-hw 30 sh -c 'for n in $1; do printf "%s: " "$n"; sysctl -n "$n" 2>&1; done' sh \
    "hw.model hw.ncpu hw.memsize machdep.cpu.brand_string kern.hv_support kern.hv_vmm_present kern.bootargs"
step csrutil 30 csrutil status
step nvram-boot-args 30 nvram boot-args
step sysext 60 systemextensionsctl list
step ps 30 ps -axo pid,user,comm
step sudo 30 sudo -n id
step tracers 30 ls -l /usr/bin/fs_usage /usr/bin/ktrace /usr/sbin/dtrace /usr/bin/eslogger \
    /usr/bin/iosnoop /usr/bin/iotop
for p in fs_usage ktrace dtrace eslogger iosnoop; do
    step "man-$p" 60 sh -c 'MANPAGER=cat man "$1" | col -bx' sh "$p"
done
for p in fcntl getattrlist; do
    step "man-$p" 60 sh -c 'MANPAGER=cat man 2 "$1" | col -bx' sh "$p"
done

section knobs
step sysctl-apfs 60 sh -c 'sysctl -a 2>/dev/null | grep -i -E "apfs|vfs\.generic"'
step sysctl-count 60 sh -c 'sysctl -a 2>/dev/null | wc -l'

section base
step l2p-cc 120 cc -O2 -Wall -o "$WORK/l2p" "$HERE/l2p.c"
base=$(newfs_file base 64m -v Observe) || base=""
hfsbase="$WORK/hfs-base.img"
if step hfs-base-mkfile 30 mkfile -n 64m "$hfsbase" && hdev=$(attach_raw hfs-base "$hfsbase"); then
    step hfs-base-newfs 120 newfs_hfs -v Control "$hdev" ||
        sstep hfs-base-newfs-root 120 newfs_hfs -v Control "$hdev" || hfsbase=""
    detach hfs-base "$hdev"
else
    hfsbase=""
fi

section fs_usage
if [ -n "$base" ]; then
    tracer fsu 240 "$OUT/fsu.out" fs_usage -w -f diskio && op fsu "$base" apfs
    tracer_stop fs_usage
    [ -s "$OUT/op-fsu.vol" ] &&
        step fsu-match 120 "$PY" "$OBS" match-text "$OUT/fsu.out" "$OUT/log/fsu-l2p.out" \
            "$(cat "$OUT/op-fsu.vol")" D= "$OUT/fsu-match.json"
    [ -s "$OUT/op-fsu.cont" ] &&
        step fsu-match-container 120 "$PY" "$OBS" match-text "$OUT/fsu.out" "$OUT/log/fsu-l2p.out" \
            "$(cat "$OUT/op-fsu.cont")" D= "$OUT/fsu-match-container.json"
fi

section dtrace
sstep dtrace-count 120 sh -c 'dtrace -l | wc -l'
sstep dtrace-io-list 60 dtrace -l -P io
cat >"$WORK/io.d" <<'EOF'
io:::start
{
    printf("%d %s %s blk=%d bytes=%d flags=0x%x\n", timestamp, execname,
        args[1]->dev_statname, args[0]->b_blkno, args[0]->b_bcount, args[0]->b_flags);
}
EOF
if [ -n "$base" ]; then
    tracer dtrace-io 240 "$OUT/dtrace-io.out" dtrace -q -s "$WORK/io.d" && op dtrace "$base" apfs
    tracer_stop dtrace
    [ -s "$OUT/log/dtrace-l2p.out" ] &&
        step dtrace-match 120 "$PY" "$OBS" match-text "$OUT/dtrace-io.out" "$OUT/log/dtrace-l2p.out" \
            - blk= "$OUT/dtrace-match.json"
fi

section ktrace
if [ -n "$hfsbase" ]; then
    kt="$WORK/kt-all.ndjson"
    if tracer kt-all 240 "$kt" ktrace trace -f ALL --ndjson; then
        op ktall "$hfsbase" hfs
    else
        tracer_stop ktrace
        tracer kt-default 240 "$kt" ktrace trace --ndjson && op ktall "$hfsbase" hfs
    fi
    tracer_stop ktrace
    step kt-all-size 30 ls -l "$kt"
    [ -s "$OUT/log/ktall-l2p.out" ] &&
        step kt-all-match 1200 "$PY" "$OBS" match-ktrace "$kt" "$OUT/log/ktall-l2p.out" \
            "$OUT/ktrace-control.json" "$OUT/ktrace-control.ndjson"
    rm -f "$kt"
fi
filter=$(tail -n 1 "$OUT/log/kt-all-match.out" 2>/dev/null)
if [[ $filter =~ ^S0x[0-9a-f]{4}(,S0x[0-9a-f]{4})*$ ]]; then
    i=0
    for code in $("$PY" -I -c 'import json, sys
for m in json.load(open(sys.argv[1]))["matching"]: print(m["code"])' "$OUT/ktrace-control.json"); do
        i=$((i + 1))
        step "kt-decode-$i" 30 ktrace decode "$code" || sstep "kt-decode-$i-root" 30 ktrace decode "$code"
    done
    if [ -n "$base" ]; then
        sstep ktrace-remove-control 30 ktrace remove
        tracer kt-apfs 240 "$OUT/ktrace-apfs.ndjson" ktrace trace -f "$filter" --ndjson &&
            op kt "$base" apfs
        tracer_stop ktrace
        [ -s "$OUT/log/kt-l2p.out" ] &&
            step kt-apfs-match 600 "$PY" "$OBS" match-ktrace "$OUT/ktrace-apfs.ndjson" \
                "$OUT/log/kt-l2p.out" "$OUT/ktrace-apfs.json" - --all-codes
    fi
else
    step kt-no-filter 5 sh -c 'echo "the control trace gave no filter: $1"; exit 1' sh "$filter"
fi
sstep ktrace-remove 30 ktrace remove

section log
sstep log-status-0 30 log config --status
sstep log-mode-system 30 log config --mode "level:debug"
sstep log-mode-apfs 30 log config --subsystem com.apple.apfs --mode "level:debug,persist:debug"
sstep log-status-1 30 log config --status
started=$(date '+%Y-%m-%d %H:%M:%S')
pred='senderImagePath CONTAINS[c] "apfs" OR subsystem BEGINSWITH "com.apple.apfs" OR process IN {"fsck_apfs","newfs_apfs","mount_apfs","hdiutil","diskimagesiod","diskarbitrationd"}'
if [ -n "$base" ]; then
    if tracer log-stream 240 "$OUT/log-stream.ndjson" log stream --level debug --style ndjson \
        --predicate "$pred"; then
        fmt=$(newfs_file logfmt 64m -v LogProbe) &&
            step log-fsck 300 fsck_apfs -n -W -d -s -c "$fmt"
        op log "$base" apfs
    fi
    tracer_stop log
fi
sstep log-show 600 sh -c 'log show --info --debug --style ndjson --start "$1" --predicate "$2" >"$3"' \
    sh "$started" "$pred" "$OUT/log-show.ndjson"
step log-types 120 "$PY" "$OBS" log-types "$OUT/log-show.ndjson"
sstep fsck-logs 30 sh -c 'ls -l /var/log/fsck_apfs*; tail -n 200 /var/log/fsck_apfs.log /var/log/fsck_apfs_error.log'
sstep log-reset-apfs 30 log config --subsystem com.apple.apfs --reset
sstep log-reset 30 log config --reset
sstep log-status-2 30 log config --status

section tools
if [ -n "$base" ] && step tools-copy 60 cp "$base" "$WORK/tools.img" &&
    dev=$(attach_raw tools "$WORK/tools.img"); then
    step ioreg-container 60 ioreg -r -c AppleAPFSContainer -l -w0
    step ioreg-volume 60 ioreg -r -c AppleAPFSVolume -l -w0
    if cont=$(container_of tools-list "$dev") && vol=$(first_volume tools-list "$cont"); then
        sstep checkseal-d 120 "$RES/apfs_checkseal" -d -q -v "/dev/$vol"
    fi
    sstep stats-e 30 "$RES/apfs_stats" -e
    sstep smstats-v 60 "$RES/sm_stats" -v "/dev/r${dev#/dev/}"
    detach tools "$dev"
fi

section vm
step hv-cc 120 cc -o "$WORK/hvcreate" "$HERE/hvcreate.c" -framework Hypervisor
cat >"$WORK/hv.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>com.apple.security.hypervisor</key><true/>
<key>com.apple.security.virtualization</key><true/>
</dict></plist>
EOF
step hv-sign 60 codesign -s - -f --entitlements "$WORK/hv.plist" "$WORK/hvcreate"
step hv-run 60 "$WORK/hvcreate"
step vz-run 300 xcrun swift "$HERE/vzsupport.swift"
step cpu-vmx 30 sh -c 'sysctl -n machdep.cpu.features 2>/dev/null | tr " " "\n" | grep -x VMX || echo "no VMX flag"'

section es
step es-list 60 eslogger --list-events
if [ -n "$base" ]; then
    tracer es 240 "$OUT/es.ndjson" eslogger create write close rename unlink setextattr clone &&
        op es "$base" apfs
    tracer_stop eslogger
fi
step xctrace-templates 120 xcrun xctrace list templates

section done
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
exit 0
