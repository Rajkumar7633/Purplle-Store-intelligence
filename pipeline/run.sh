#!/bin/bash
# One command to process all CCTV clips and ingest events into the API.
# Usage: ./pipeline/run.sh [clips_dir] [store_id] [api_url]
# Example: ./pipeline/run.sh ./clips STORE_BLR_002 http://localhost:8000

set -euo pipefail

CLIPS_DIR="${1:-./clips}"
STORE_ID="${2:-STORE_BLR_002}"
API_URL="${3:-http://localhost:8000}"
LAYOUT="${4:-./data/store_layout.json}"
POS_CSV="${5:-./data/pos_transactions.csv}"
OUTPUT_DIR="./output/${STORE_ID}"
MODEL="${YOLO_MODEL:-yolov8n.pt}"
CONF="${DETECTION_CONFIDENCE:-0.35}"
DEVICE="${DETECTION_DEVICE:-cpu}"

echo "================================================"
echo "Store Intelligence Pipeline"
echo "Store: ${STORE_ID} | API: ${API_URL}"
echo "Clips: ${CLIPS_DIR} | Output: ${OUTPUT_DIR}"
echo "================================================"

mkdir -p "${OUTPUT_DIR}"
shopt -s nullglob

# Camera to clip mapping — matches actual clip file naming conventions
# This allows files like "CAM 1 - zone.mp4" and "CAM 3 - entry.mp4" to be found.
CAMERA_IDS=(
    "CAM_ENTRY_01"
    "CAM_FLOOR_01"
    "CAM_FLOOR_02"
    "CAM_BILLING_01"
    "CAM_FLOOR_03"
)
CAMERA_PATTERNS=(
    "CAM 1*.mp4"
    "CAM 2*.mp4"
    "CAM 3*.mp4"
    "CAM 4*.mp4"
    "CAM 5*.mp4"
)

EVENTS_FILE="${OUTPUT_DIR}/events.jsonl"
> "${EVENTS_FILE}"  # clear/create

clip_files=("${CLIPS_DIR}"/*.mp4)
if [ "${#clip_files[@]}" -eq 0 ]; then
    echo "ERROR: No .mp4 clips found in '${CLIPS_DIR}'"
    exit 1
fi

for clip in "${clip_files[@]}"; do
    if [ ! -f "${clip}" ]; then
        continue
    fi

    filename="$(basename "${clip}")"
    filename_lower="$(printf '%s' "${filename}" | tr '[:upper:]' '[:lower:]')"

    if [[ "${filename_lower}" == *entry* ]]; then
        CAMERA_ID="CAM_ENTRY_01"
    elif [[ "${filename_lower}" == *billing* ]]; then
        CAMERA_ID="CAM_BILLING_01"
    elif [[ "${filename_lower}" == *cam 1* ]]; then
        CAMERA_ID="CAM_FLOOR_01"
    elif [[ "${filename_lower}" == *cam 2* ]]; then
        CAMERA_ID="CAM_FLOOR_02"
    elif [[ "${filename_lower}" == *cam 4* ]]; then
        CAMERA_ID="CAM_FLOOR_03"
    elif [[ "${filename_lower}" == *cam 5* ]]; then
        CAMERA_ID="CAM_FLOOR_03"
    else
        CAMERA_ID="CAM_FLOOR_03"
    fi

    echo "Processing ${CAMERA_ID}: ${clip}"
    CAMERA_OUTPUT="${OUTPUT_DIR}/events_${CAMERA_ID}.jsonl"
    START_TIME=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    python3 pipeline/detect.py \
        --clip "${CLIP}" \
        --store-id "${STORE_ID}" \
        --camera-id "${CAMERA_ID}" \
        --layout "${LAYOUT}" \
        --output "${CAMERA_OUTPUT}" \
        --start-time "${START_TIME}" \
        --model "${MODEL}" \
        --conf "${CONF}" \
        --device "${DEVICE}"

    # Merge into combined events file
    cat "${CAMERA_OUTPUT}" >> "${EVENTS_FILE}"
    echo "  → Events written to ${CAMERA_OUTPUT}"
done

echo ""
echo "Ingesting events into API..."
python3 pipeline/ingest_to_api.py \
    --events "${EVENTS_FILE}" \
    --api "${API_URL}"

if [ -f "${POS_CSV}" ]; then
    echo "Ingesting POS transactions..."
    python pipeline/ingest_to_api.py \
        --pos "${POS_CSV}" \
        --api "${API_URL}"
fi

echo ""
echo "Done! Check metrics at ${API_URL}/stores/${STORE_ID}/metrics"
