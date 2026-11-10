#!/bin/sh
# run_gate.sh: the test gate in parallel processes. Every tests/test_*.py runs as its
# own 'python3 -m unittest tests.<module>' process. Phase 1 runs the modules that are
# safe side by side, a few at a time; phase 2 then runs the timing-sensitive ones
# (signals, process groups, locks, sleeps and grace periods) one at a time, alone,
# as the sequential run does. The reference, and what CI runs, stays
# 'python3 -m unittest discover -s tests'.
#
# Usage: sh tests/run_gate.sh [-j N | --jobs N]
# The number of phase 1 processes at a time is N, else VIDFUNNEL_GATE_JOBS, else 4.
# Logs go to a temporary folder that is removed when every module passes and kept
# (its path printed) otherwise. Exit status 0 only when every module passed.

VERSION="20261110"

set -u
unset CDPATH

# The timing-sensitive modules: run in phase 2, one at a time. A name here whose
# module no longer exists is reported and skipped.
SERIAL_MODULES="
test_cli            sends SIGTERM to itself and to child processes and times the handler
test_interrupt      signals and grace periods around real ffmpeg encodes
test_record         record locks, forked lock holders and pid liveness
test_retire         SIGUSR1 and stop files to a process group with a sleeping par2 stand-in
test_verify         SIGUSR1 and stop files to a process group with a sleeping ffmpeg stand-in
test_stopfile       installs and restores the process's signal handlers
test_podman_wrapper SIGINT and SIGHUP to process groups, polled deadlines and stop delays
test_shared_library two convert processes share one library through folder locks and gate files
"

jobs="${VIDFUNNEL_GATE_JOBS:-4}"
while [ $# -gt 0 ]; do
    case "$1" in
        -j|--jobs)
            [ $# -ge 2 ] || { echo "run_gate: '$1' needs a number" >&2; exit 2; }
            jobs="$2"
            shift 2
            ;;
        --jobs=*)
            jobs="${1#--jobs=}"
            shift
            ;;
        -h|--help)
            sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        --version)
            echo "run_gate.sh $VERSION"
            exit 0
            ;;
        *)
            echo "run_gate: unknown argument '$1' (try --help)" >&2
            exit 2
            ;;
    esac
done
case "$jobs" in
    ''|*[!0-9]*|0) echo "run_gate: the number of processes must be a whole number of 1 or more, not '$jobs'" >&2; exit 2 ;;
esac

self="$0"
while [ -L "$self" ]; do
    target=$(readlink "$self")
    case "$target" in
        /*) self="$target" ;;
        *) self="$(dirname "$self")/$target" ;;
    esac
done
testsdir=$(cd "$(dirname "$self")" && pwd -P) || exit 2
root=$(dirname "$testsdir")
cd "$root" || exit 2

if ! command -v python3 >/dev/null 2>&1; then
    echo "run_gate: python3 was not found on PATH" >&2
    exit 2
fi

tmp=$(mktemp -d "${TMPDIR:-/tmp}/vidfunnel-gate.XXXXXX") || { echo "run_gate: could not make a temporary folder" >&2; exit 2; }
# Bytecode goes to the temporary folder, not into the repo's __pycache__ folders.
PYTHONPYCACHEPREFIX="$tmp/pycache"
export PYTHONPYCACHEPREFIX

# Each background job gets its own process group, so an interrupt can stop a
# module's python and everything it started, and SIGINT is not ignored in it.
set -m

now() {
    date +%s
}

is_serial() {
    echo "$SERIAL_MODULES" | while read -r name _reason; do
        [ "$name" = "$1" ] && echo yes && break
    done
}

all=""
for path in "$testsdir"/test_*.py; do
    [ -f "$path" ] || continue
    name=$(basename "$path" .py)
    all="$all $name"
done
if [ -z "$all" ]; then
    echo "run_gate: no test modules found in '$testsdir'" >&2
    rm -rf "$tmp"
    exit 2
fi

# Phase 1 starts the largest files first, so the long modules do not start last.
parallel=$(for name in $all; do
    [ -n "$(is_serial "$name")" ] && continue
    echo "$(wc -c < "$testsdir/$name.py") $name"
done | sort -rn | while read -r _size name; do printf '%s ' "$name"; done)
serial=""
missing=""
for name in $(echo "$SERIAL_MODULES" | while read -r name _reason; do [ -n "$name" ] && echo "$name"; done); do
    if [ -f "$testsdir/$name.py" ]; then
        serial="$serial $name"
    else
        missing="$missing $name"
    fi
done

running=""      # "module:pid" pairs; pid is also the job's process group
failed=""
modules=0
total_tests=0
total_skips=0
total_failures=0
total_errors=0

stop_running() {
    trap '' INT TERM HUP
    echo "run_gate: interrupted; stopping the running modules" >&2
    for entry in $running; do
        kill -s INT -- "-${entry#*:}" 2>/dev/null
    done
    waited=0
    while [ $waited -lt 10 ]; do
        alive=""
        for entry in $running; do
            kill -s 0 -- "-${entry#*:}" 2>/dev/null && alive=yes
        done
        [ -z "$alive" ] && break
        sleep 1
        waited=$((waited + 1))
    done
    for entry in $running; do
        kill -s KILL -- "-${entry#*:}" 2>/dev/null
    done
    wait 2>/dev/null
    echo "run_gate: interrupted; logs kept in '$tmp'" >&2
    exit 130
}
trap stop_running INT TERM HUP

run_module() {
    python3 -m unittest "tests.$1" </dev/null >"$tmp/$1.log" 2>&1
    echo $? >"$tmp/$1.rc.part"
    mv "$tmp/$1.rc.part" "$tmp/$1.rc"
}

start_module() {
    echo "[start] tests.$1"
    now >"$tmp/$1.start"
    run_module "$1" &
    running="$running $1:$!"
}

# Report a finished module and add its counts to the totals.
finish_module() {
    module="$1"
    log="$tmp/$module.log"
    status=$(cat "$tmp/$module.rc")
    seconds=$(( $(now) - $(cat "$tmp/$module.start") ))
    ran=$(sed -n 's/^Ran \([0-9][0-9]*\) tests\{0,1\} in .*/\1/p' "$log" | tail -n 1)
    verdict=$(grep -E '^(OK|FAILED)( \(.*\))?$' "$log" | tail -n 1)
    skips=$(echo "$verdict" | sed -n 's/.*skipped=\([0-9][0-9]*\).*/\1/p')
    failures=$(echo "$verdict" | sed -n 's/.*failures=\([0-9][0-9]*\).*/\1/p')
    errors=$(echo "$verdict" | sed -n 's/.*errors=\([0-9][0-9]*\).*/\1/p')
    modules=$((modules + 1))
    total_tests=$((total_tests + ${ran:-0}))
    total_skips=$((total_skips + ${skips:-0}))
    total_failures=$((total_failures + ${failures:-0}))
    total_errors=$((total_errors + ${errors:-0}))
    if [ "$status" = 0 ] && [ -n "$ran" ] && [ -n "$verdict" ]; then
        result="$verdict"
    elif [ "$status" = 0 ]; then
        # A process that ends with status 0 before unittest reports (os._exit(0) in a test) is no pass.
        result="exit status 0 without a unittest summary ('Ran N tests' and OK or FAILED)"
        failed="$failed $module"
    else
        result="${verdict:-no unittest summary}, exit status $status"
        failed="$failed $module"
    fi
    echo "[done]  tests.$module: ${seconds} s, ${ran:-0} tests, $result"
}

# Run the given modules, at most $1 at a time; return when all have finished.
run_pool() {
    limit="$1"
    shift
    queue="$*"
    while [ -n "$queue" ] || [ -n "$running" ]; do
        still=""
        count=0
        for entry in $running; do
            module="${entry%%:*}"
            if [ -f "$tmp/$module.rc" ]; then
                wait "${entry#*:}" 2>/dev/null
                finish_module "$module"
            else
                still="$still $entry"
                count=$((count + 1))
            fi
        done
        running="$still"
        while [ -n "$queue" ] && [ $count -lt "$limit" ]; do
            set -- $queue
            next="$1"
            shift
            queue="$*"
            start_module "$next"
            count=$((count + 1))
        done
        [ -n "$running" ] && { sleep 0.2 2>/dev/null || sleep 1; }
    done
}

started=$(now)
echo "run_gate.sh $VERSION in '$root'"
for name in $missing; do
    echo "run_gate: note: the serial list names '$name', which does not exist; skipped"
done

# Warm the ffmpeg capability cache once, so the parallel modules read it instead of
# all querying ffmpeg and writing the same cache file at the same moment.
if python3 -c 'from vidfunnel.ffmpeg import capabilities, find_tools; capabilities(find_tools())' \
        </dev/null >"$tmp/warm.log" 2>&1; then
    echo "[warm]  ffmpeg capability cache ready"
else
    echo "[warm]  no ffmpeg capabilities (the ffmpeg tests will skip or say why):"
    sed 's/^/        /' "$tmp/warm.log"
fi

set -- $parallel
echo "[phase 1] $# modules, $jobs at a time"
run_pool "$jobs" $parallel
set -- $serial
echo "[phase 2] $# timing-sensitive modules, one at a time"
run_pool 1 $serial

elapsed=$(( $(now) - started ))
wall="$((elapsed / 60)) min $((elapsed % 60)) s"
trap - INT TERM HUP

for module in $failed; do
    echo
    echo "===== tests.$module: full output ====="
    cat "$tmp/$module.log"
done
echo
summary="$modules modules, $total_tests tests, $total_skips skipped, $total_failures failures, $total_errors errors, $wall"
if [ -z "$failed" ]; then
    rm -rf "$tmp"
    echo "run_gate: $summary: OK"
    exit 0
fi
echo "run_gate: failed modules:$failed"
echo "run_gate: logs kept in '$tmp'"
echo "run_gate: $summary: FAILED"
exit 1

# End of File #
