#!/usr/bin/env bash
# 0단계에서 돌리는 점검. yt-dlp · ffmpeg · 파이썬에서 paddleocr · cv2 불러오기 · node · CPU 수를 한 줄씩 통과 또는 실패로 찍는다.
# 쓰는 법: bash check_env.sh
# paddleocr 를 못 부르면 ~/.cache/video-to-claude/pyfix 를 PYTHONPATH 앞에 넣고 한 번 더 본다.
# 그래도 실패하면 ~/.local 은 건드리지 않는 고치는 명령을 안내하고 종료 코드 1 로 끝낸다.
set -u

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

ERR="$(mktemp /tmp/check_env_ocr.XXXXXX)"
if python3 -c 'import paddleocr' >/dev/null 2>"$ERR"; then
  pass "paddleocr 불러오기"
else
  last="$(tail -n 1 "$ERR" 2>/dev/null || true)"
  # 여기서 failed 를 올리지 않는다. pyfix 로 다시 부르면 통과다. 다시 부르기도 실패할 때만 실패로 센다.
  echo "실패  paddleocr 불러오기 — ${last:-불러오지 못했다}"
  if PYTHONPATH="${PYFIX}${PYTHONPATH:+:$PYTHONPATH}" python3 -c 'import paddleocr' >/dev/null 2>"$ERR"; then
    pass "paddleocr 불러오기 — PYTHONPATH 에 ${PYFIX} 를 넣고 다시 불렀다"
  else
    last="$(tail -n 1 "$ERR" 2>/dev/null || true)"
    fail "paddleocr 불러오기 — ${PYFIX} 를 PYTHONPATH 에 넣어도 실패 — ${last:-불러오지 못했다}"
    if printf '%s' "$last" | grep -q "No module named 'paddleocr'\|ModuleNotFoundError: .*paddleocr"; then
      echo "paddleocr 자체가 없다. 설치 명령 = python3 -m pip install --user paddleocr paddlepaddle"
    elif printf '%s' "$last" | grep -qi "urllib3\|requests"; then
      echo "고치는 명령 = python3 -m pip install --target \"${PYFIX}\" 'urllib3>=2' requests"
      echo "그 다음 = PYTHONPATH=\"${PYFIX}\" python3 -c 'import paddleocr'"
      echo "사용자 파이썬 부품 폴더(~/.local)는 고치지 않는다."
    else
      echo "원인을 위 오류 줄에서 확인한다. paddleocr 가 없으면 python3 -m pip install --user paddleocr paddlepaddle, urllib3 계열 오류면 python3 -m pip install --target \"${PYFIX}\" 'urllib3>=2' requests 를 쓴다."
    fi
  fi
fi
rm -f "$ERR"

# 장면 뽑기(sample_frames.py)와 글자 인식(screen_ocr.py)은 영상 장면을 cv2 로 읽는다
if python3 -c 'import cv2' >/dev/null 2>&1; then
  pass "cv2 불러오기"
else
  fail "cv2 불러오기 — 장면 뽑기와 화면 글자 인식을 못 한다"
  echo "설치 명령 = python3 -m pip install --user opencv-python"
fi

# 소리 받아쓰기(transcribe.py)를 할 때만 필요하다. 없어도 실패로 세지 않는다(종료 코드에 영향 없음).
if python3 -c 'import faster_whisper' >/dev/null 2>&1; then
  pass "faster-whisper 불러오기"
else
  echo "알림  faster-whisper 없음 — 소리 받아쓰기를 할 때만 필요하다. 설치 명령 = python3 -m pip install --user faster-whisper"
fi

# 유튜브는 영상 주소를 풀 때 자바스크립트 실행기가 필요하다(fetch.sh 가 node 를 넘긴다)
if command -v node >/dev/null 2>&1; then
  pass "node = $(command -v node)"
else
  fail "node 없음 — 유튜브 받기가 막힌다"
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
