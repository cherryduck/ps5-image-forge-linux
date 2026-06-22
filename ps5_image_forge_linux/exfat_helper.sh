#!/bin/bash
# exfat_helper.sh - Runs ALL privileged EXFAT operations in a single pkexec session.
#
# Single-item mode (backward compatible):
#   pkexec exfat_helper.sh <image_path> <source_dir> <mount_point> <uid> <gid> [progress_file]
#
# Batch mode (single auth for multiple builds):
#   pkexec exfat_helper.sh --batch "image1|source1|uid|gid|progress1" "image2|source2|uid|gid|progress2" ...
#
# In batch mode each argument is a pipe-delimited tuple:
#   image_path | source_dir | uid | gid | progress_file
# progress_file is optional (may be empty string).

set -euo pipefail

# ---------------------------------------------------------------------------
# Batch mode
# ---------------------------------------------------------------------------
if [ "${1:-}" = "--batch" ]; then
    shift
    # Process each item; use set +e so one failure doesn't abort the rest
    set +e
    for ITEM in "$@"; do
        IFS='|' read -r IMAGE SOURCE UID_VAL GID_VAL PROGRESS_FILE <<< "$ITEM"

        MOUNT=$(mktemp -d --suffix="=exfat_mount")
        LOOP_DEV=""

        # Per-item cleanup
        item_cleanup() {
            umount "$MOUNT" 2>/dev/null || umount -l "$MOUNT" 2>/dev/null || true
            [ -n "$LOOP_DEV" ] && losetup -d "$LOOP_DEV" 2>/dev/null || true
            rm -rf "$MOUNT" 2>/dev/null || true
        }
        trap item_cleanup EXIT

        # 1. Attach loop device
        LOOP_DEV=$(losetup --find --show "$IMAGE" 2>&1) || {
            echo "BATCH_ERROR:${IMAGE}:losetup failed: $LOOP_DEV" >&2
            continue
        }

        # 2. Mount
        mount -o "uid=${UID_VAL},gid=${GID_VAL}" "$LOOP_DEV" "$MOUNT" 2>/dev/null || {
            echo "BATCH_ERROR:${IMAGE}:mount failed" >&2
            losetup -d "$LOOP_DEV" 2>/dev/null || true
            rm -rf "$MOUNT" 2>/dev/null || true
            continue
        }

        # 3. Copy files
        TOTAL_BYTES=$(du -sb "$SOURCE" | awk '{print $1}')

        # Background progress writer (if progress file requested)
        PROGRESS_PID=""
        if [ -n "$PROGRESS_FILE" ]; then
            (
                sleep 0.5
                BASELINE=$(df --output=used "$MOUNT" 2>/dev/null | tail -1 | tr -d ' ' || echo "0")
                while true; do
                    USED=$(df --output=used "$MOUNT" 2>/dev/null | tail -1 | tr -d ' ' || echo "0")
                    COPIED=$((USED - BASELINE))
                    COPIED_BYTES=$((COPIED * 1024))
                    if [ "$TOTAL_BYTES" -gt 0 ] 2>/dev/null; then
                        PCT=$((COPIED_BYTES * 100 / TOTAL_BYTES))
                        [ "$PCT" -gt 100 ] && PCT=100
                        echo "$PCT" > "$PROGRESS_FILE"
                    fi
                    sleep 0.5
                done
            ) &
            PROGRESS_PID=$!
        fi

        rsync -a --info=progress2 "$SOURCE"/ "$MOUNT"/ >/dev/null 2>&1
        COPY_EXIT=$?

        # Kill progress writer
        if [ -n "$PROGRESS_PID" ]; then
            kill "$PROGRESS_PID" 2>/dev/null || true
            wait "$PROGRESS_PID" 2>/dev/null || true
            # Write final progress
            [ -n "$PROGRESS_FILE" ] && echo "100" > "$PROGRESS_FILE"
        fi

        if [ $COPY_EXIT -ne 0 ]; then
            echo "BATCH_ERROR:${IMAGE}:rsync failed with exit code $COPY_EXIT" >&2
            item_cleanup
            continue
        fi

        # 4. Sync
        sync

        # 5. Clean up this item
        item_cleanup
        echo "BATCH_OK:${IMAGE}" >&2
    done
    # Clear the trap so the outer cleanup doesn't run
    trap - EXIT
    exit 0
fi

# ---------------------------------------------------------------------------
# Single-item mode (original behavior — unchanged)
# ---------------------------------------------------------------------------
IMAGE="$1"
SOURCE="$2"
MOUNT="$3"
UID_VAL="${4:-$(id -u)}"
GID_VAL="${5:-$(id -g)}"
PROGRESS_FILE="${6:-}"

LOOP_DEV=""
PROGRESS_PID=""

cleanup() {
    # 1. Kill background progress writer first (it holds refs to the mount point)
    if [ -n "$PROGRESS_PID" ]; then
        kill "$PROGRESS_PID" 2>/dev/null || true
        wait "$PROGRESS_PID" 2>/dev/null || true
    fi
    # 2. Unmount (lazy fallback if busy)
    umount "$MOUNT" 2>/dev/null || umount -l "$MOUNT" 2>/dev/null || true
    # 3. Detach loop device
    if [ -n "$LOOP_DEV" ]; then
        losetup -d "$LOOP_DEV" 2>/dev/null || true
    fi
    # 4. Remove mount point directory
    rm -rf "$MOUNT" 2>/dev/null || true
}

trap cleanup EXIT

# 1. Attach loop device
LOOP_DEV=$(losetup --find --show "$IMAGE")
if [ -z "$LOOP_DEV" ]; then
    echo "ERROR: losetup returned no device" >&2
    exit 1
fi

# 2. Mount
mount -o "uid=${UID_VAL},gid=${GID_VAL}" "$LOOP_DEV" "$MOUNT"
if [ $? -ne 0 ]; then
    echo "ERROR: mount failed" >&2
    exit 1
fi

# 3. Copy files from source to mount point
# Calculate total source size for progress tracking
TOTAL_BYTES=$(du -sb "$SOURCE" | awk '{print $1}')

# Background progress writer (if progress file requested)
if [ -n "$PROGRESS_FILE" ]; then
    (
        # Get initial used space (filesystem metadata only)
        sleep 0.5
        BASELINE=$(df --output=used "$MOUNT" 2>/dev/null | tail -1 | tr -d ' ' || echo "0")
        while true; do
            USED=$(df --output=used "$MOUNT" 2>/dev/null | tail -1 | tr -d ' ' || echo "0")
            COPIED=$((USED - BASELINE))
            # Convert 1K blocks to bytes
            COPIED_BYTES=$((COPIED * 1024))
            if [ "$TOTAL_BYTES" -gt 0 ] 2>/dev/null; then
                PCT=$((COPIED_BYTES * 100 / TOTAL_BYTES))
                [ "$PCT" -gt 100 ] && PCT=100
                echo "$PCT" > "$PROGRESS_FILE"
            fi
            sleep 0.5
        done
    ) &
    PROGRESS_PID=$!
fi

rsync -a --info=progress2 "$SOURCE"/ "$MOUNT"/ >/dev/null 2>&1
COPY_EXIT=$?

if [ $COPY_EXIT -ne 0 ]; then
    echo "ERROR: rsync failed with exit code $COPY_EXIT" >&2
    exit $COPY_EXIT
fi

# 4. Sync to ensure all data is flushed
sync

# Cleanup runs automatically via trap (umount + losetup -d)
exit 0
