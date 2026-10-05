#!/usr/bin/env bash
# 0단계에서 돌리는 점검. 꼭 필요한 것은 yt-dlp · ffmpeg · python3다.
# paddleocr · cv2 · faster-whisper · node 는 없으면 알림과 설치 안내만 찍고 종료 코드에는 영향 없다.
# 쓰는 법: bash check_env.sh
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
SKILL_ROOT="$(cd "$HERE/.." && pwd)"

failed=0
pass() { echo "통과  $*"; }
fail() { echo "실패  $*"; failed=1; }

CACHE="${HOME}/.cache/video-to-claude"
PYFIX="${CACHE}/pyfix"
YT=""
if [ -x "${CACHE}/yt-dlp" ]; then
  YT="${CACHE}/yt-dlp"
elif command -v yt-dlp >/dev/null 2>&1; then
  YT="$(command -v yt-dlp)"
fi
if [ -n "$YT" ]; then
  pass "yt-dlp = ${YT}"
else
  fail "yt-dlp 없음 — ${CACHE}/yt-dlp 도 PATH 에도 없다"
fi

FF=""
if command -v ffmpeg >/dev/null 2>&1; then
  FF="$(command -v ffmpeg)"
fi
if [ -n "$FF" ]; then
  pass "ffmpeg = ${FF}"
else
  fail "ffmpeg 없음"
fi

if command -v python3 >/dev/null 2>&1; then
  pass "python3 = $(command -v python3)"
else
  fail "python3 없음"
fi

ERR="$(mktemp /tmp/check_env_ocr.XXXXXX)"
if python3 -c 'import paddleocr' >/dev/null 2>"$ERR"; then
  pass "paddleocr 불러오기"
elif PYTHONPATH="${PYFIX}${PYTHONPATH:+:$PYTHONPATH}" python3 -c 'import paddleocr' >/dev/null 2>"$ERR"; then
  pass "paddleocr 불러오기 — PYTHONPATH 에 ${PYFIX} 를 넣고 다시 불렀다"
else
  last="$(tail -n 1 "$ERR" 2>/dev/null || true)"
  echo "알림  paddleocr 없음 — 화면 글자 인식을 할 때 필요하다. 설치 명령 = python3 -m pip install --user -r \"${SKILL_ROOT}/requirements-ocr.txt\"${last:+ — }${last}"
fi
rm -f "$ERR"

# 장면 뽑기(sample_frames.py)와 글자 인식(screen_ocr.py)은 영상 장면을 cv2 로 읽는다
if python3 -c 'import cv2' >/dev/null 2>&1; then
  pass "cv2 불러오기"
else
  echo "알림  cv2 없음 — 장면 뽑기와 화면 글자 인식을 할 때 필요하다. 설치 명령 = python3 -m pip install --user -r \"${SKILL_ROOT}/requirements-ocr.txt\""
fi

# 소리 받아쓰기(transcribe.py)를 할 때만 필요하다. 없어도 실패로 세지 않는다(종료 코드에 영향 없음).
if python3 -c 'import faster_whisper' >/dev/null 2>&1; then
  pass "faster-whisper 불러오기"
else
  echo "알림  faster-whisper 없음 — 소리 받아쓰기를 할 때만 필요하다. 설치 명령 = python3 -m pip install --user -r \"${SKILL_ROOT}/requirements-whisper.txt\""
fi

# 유튜브 영상을 받을 때만 필요하다(fetch.sh 가 node 를 넘긴다). 없어도 종료 코드에는 영향 없다.
if command -v node >/dev/null 2>&1; then
  pass "node = $(command -v node)"
else
  echo "알림  node 없음 — 유튜브를 받을 때 필요하다"
fi

CPU="$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || true)"
if [ -n "${CPU}" ]; then
  pass "CPU 수 = ${CPU}"
else
  fail "CPU 수를 읽지 못했다"
fi

# 1분 부하. 리눅스는 /proc/loadavg, 그 밖은 sysctl 또는 uptime. 못 읽으면 이 줄만 건너뛴다.
LOAD1=""
if [ -r /proc/loadavg ]; then
  LOAD1="$(awk '{print $1}' /proc/loadavg 2>/dev/null || true)"
else
  if command -v sysctl >/dev/null 2>&1; then
    raw="$(sysctl -n vm.loadavg 2>/dev/null || true)"
    LOAD1="$(printf '%s\n' "$raw" | awk '{gsub(/[{}]/, ""); print $1}')"
  fi
  if [ -z "$LOAD1" ]; then
    raw="$(uptime 2>/dev/null || true)"
    LOAD1="$(printf '%s\n' "$raw" | sed -n 's/.*load averages*: *\([0-9.][0-9.]*\).*/\1/p')"
  fi
fi
case "$LOAD1" in
  ''|*[!0-9.]*) LOAD1="" ;;
esac
if [ -n "$LOAD1" ]; then
  pass "1분 부하 = ${LOAD1}"
  if [ -n "${CPU}" ] && awk -v l="$LOAD1" -v c="$CPU" 'BEGIN{exit !(l+0 > c+0)}'; then
    echo "바쁨 — 화면 글자 동시 개수(--jobs)를 줄여라"
  fi
fi

if [ "$failed" -ne 0 ]; then
  exit 1
fi
exit 0
