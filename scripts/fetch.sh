#!/usr/bin/env bash
# 영상 주소를 받아 영상 정보·자막을 작업 폴더에 받고, --video 를 주면 글자 인식용 720p 영상 파일도 받는다
# 쓰는 법: fetch.sh <영상 주소> <작업 폴더> [--video] [--same-ok]
# --video 와 --same-ok 는 어느 자리에 있어도 된다.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
log() { echo "[fetch $(date '+%H:%M:%S %Z')] $*"; }

WANT_VIDEO=0
SAME_OK=0
POS=()
for arg in "$@"; do
  case "$arg" in
    --video) WANT_VIDEO=1 ;;
    --same-ok) SAME_OK=1 ;;
    --*)
      log "[오류] 모르는 옵션 ${arg}"
      exit 1
      ;;
    *) POS+=("$arg") ;;
  esac
done
if [ "${#POS[@]}" -lt 2 ] || [ -z "${POS[0]}" ] || [ -z "${POS[1]}" ]; then
  log "[오류] 쓰는 법 — fetch.sh <영상 주소> <작업 폴더> [--video] [--same-ok]"
  exit 1
fi
URL="${POS[0]}"
OUT="${POS[1]}"

# list= 만 있고 영상 하나(v= 또는 짧은 주소의 영상 번호)가 없으면 재생목록이다.
# 영상 번호와 list= 가 같이 있으면 그 영상만 받는다.
NO_PLAYLIST=()
playlist_rc=0
python3 - "$URL" << 'PY' || playlist_rc=$?
import sys
import urllib.parse

url = sys.argv[1].strip()
u = urllib.parse.urlparse(url)
qs = urllib.parse.parse_qs(u.query, keep_blank_values=True)
has_list = any(str(v).strip() for v in (qs.get("list") or []))
v = (qs.get("v") or [""])[0].strip()
host = (u.netloc or "").lower()
if host.startswith("www."):
    host = host[4:]
parts = [p for p in (u.path or "").split("/") if p]
path_id = ""
if host == "youtu.be" or host.endswith(".youtu.be"):
    path_id = parts[0] if parts else ""
elif len(parts) >= 2 and parts[-2] in ("shorts", "embed", "live", "v"):
    path_id = parts[-1]
if has_list and not v and not path_id:
    sys.exit(2)
if has_list and (v or path_id):
    sys.exit(3)
sys.exit(0)
PY
if [ "$playlist_rc" -eq 2 ]; then
  log "[오류] 영상 하나 주소만 받는다. 재생목록 주소(list= 만 있고 영상 번호가 없음)는 받지 않는다"
  exit 1
fi
if [ "$playlist_rc" -eq 3 ]; then
  NO_PLAYLIST=(--no-playlist)
elif [ "$playlist_rc" -ne 0 ]; then
  log "[오류] 주소에서 재생목록인지 읽지 못했다"
  exit 1
fi

# 1. 최신 yt-dlp — 깔려 있는 옛 버전은 유튜브 영상 받기가 막힌다(2026-09-07 실측).
#    없거나 7일이 지났으면 공식 배포본을 새로 받는다. 못 받으면 깔린 것으로 진행한다.
CACHE="$HOME/.cache/video-to-claude"
mkdir -p "$CACHE"
YT="$CACHE/yt-dlp"
if [ ! -x "$YT" ] || [ -n "$(find "$YT" -mtime +7 2>/dev/null)" ]; then
  log "최신 yt-dlp 받는 중"
  if curl -fsSL --retry 4 --retry-delay 3 -o "$YT.tmp" \
      https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp; then
    chmod +x "$YT.tmp" && mv "$YT.tmp" "$YT"
  else
    rm -f "$YT.tmp"
    log "[경고] 최신 yt-dlp 받기 실패 — 깔린 yt-dlp 로 진행"
  fi
fi
if [ ! -x "$YT" ]; then
  YT="$(command -v yt-dlp || true)"
fi
if [ -z "${YT:-}" ] || [ ! -x "$YT" ]; then
  log "[오류] yt-dlp 가 없다 — 인터넷이 막혔거나 안 깔렸다"
  exit 1
fi
log "yt-dlp = $YT ($("$YT" --version))"

# 유튜브는 영상 주소를 풀 때 자바스크립트 실행기가 필요하다(2026-09-07 실측 = node 지정해야 받아짐)
NODE="$(command -v node || true)"
JS=()
yt_host_rc=0
python3 - "$URL" << 'PY' || yt_host_rc=$?
import sys
import urllib.parse

url = sys.argv[1].strip()
if "://" not in url:
    url = "https://" + url
u = urllib.parse.urlparse(url)
host = (u.hostname or "").lower()
if host.startswith("www."):
    host = host[4:]
is_yt = host in (
    "youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "m.youtube-nocookie.com",
    "youtu.be",
) or host.endswith(".youtube.com") or host.endswith(".youtu.be")
sys.exit(0 if is_yt else 1)
PY
if [ "$yt_host_rc" -eq 0 ]; then
  if [ -z "$NODE" ]; then
    log "[오류] node 없음 — 유튜브 받기가 막힌다"
    exit 1
  fi
  JS=(--js-runtimes "node:$NODE")
elif [ -n "$NODE" ]; then
  JS=(--js-runtimes "node:$NODE")
fi

mkdir -p "$OUT/원본자막"

# 주소의 t= 는 아래 영상 맞춤에서 비교 키로 쓰지 않고 버린다(SKIP).
# 버리기 전에, 주소에 적힌 시각의 뜻을 받기기록과 화면에 남긴다.
note="$(python3 - "$URL" << 'PY'
import re
import sys
import urllib.parse

url = sys.argv[1]
u = urllib.parse.urlparse(url.strip())
vals = urllib.parse.parse_qs(u.query).get("t") or []
if not vals and u.fragment:
    frag = u.fragment
    vals = urllib.parse.parse_qs(frag).get("t") or []
    if not vals and frag.startswith("t="):
        vals = [frag[2:]]
if not vals:
    sys.exit(0)
raw = vals[0]


def parse_t(s):
    s = s.strip().lower()
    if re.fullmatch(r"\d+", s):
        return int(s)
    if re.fullmatch(r"\d+s", s):
        return int(s[:-1])
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", s)
    if m and any(m.groups()):
        h, mi, se = (int(x) if x else 0 for x in m.groups())
        return h * 3600 + mi * 60 + se
    return None


sec = parse_t(raw)
if sec is None:
    sys.stderr.write(f"[경고] 주소의 t={raw} 시각을 숫자로 읽지 못했다\n")
    sys.exit(0)
h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
clock = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
sys.stdout.write(f"주소에 시각 {sec}초({clock})가 있었다")
PY
)"
if [ -n "$note" ]; then
  log "$note"
  echo "$(date '+%H:%M:%S %Z') $note" >> "$OUT/원본자막/받기기록.txt"
fi

# 2. 영상 정보(이미 받았으면 다시 받지 않는다). 크기만이 아니라 JSON 으로 읽히고 id 가 있어야 한다.
#    같은 작업 폴더에 다른 영상을 받으면 예전 글이 남는다(문제 4) — 주소·번호가 다르면 멈춘다.
meta_ok() {
  python3 -c 'import json,sys
d=json.load(open(sys.argv[1]))
sys.exit(0 if d.get("id") else 1)' "$1"
}
# 0=같은 영상  1=다른 영상(stdout 에 "제목 · 번호")  2=견줄 수 없음
meta_same() {
  python3 -c 'import json,sys,urllib.parse
path,given=sys.argv[1],sys.argv[2]
d=json.load(open(path,encoding="utf-8"))
title=d.get("title")
title="(제목 없음)" if title is None or str(title).strip()=="" else " ".join(str(title).split())
stored_id=str(d.get("id") or "")
stored_url="" if d.get("webpage_url") is None else str(d.get("webpage_url"))
SKIP={"t","start","time_continue","si","feature","pp","fbclid"}
def vid_from_url(url):
    if not url: return ""
    u=urllib.parse.urlparse(url.strip())
    v=(urllib.parse.parse_qs(u.query).get("v") or [""])[0]
    if v: return v
    host=(u.netloc or "").lower()
    parts=[p for p in (u.path or "").split("/") if p]
    if not parts: return ""
    if host=="youtu.be" or host.endswith(".youtu.be"): return parts[0]
    if len(parts)>=2 and parts[-2] in ("shorts","embed","live","v"): return parts[-1]
    return ""
def url_key(url):
    u=urllib.parse.urlparse(url.strip())
    host=(u.netloc or "").lower()
    if host.startswith("www."): host=host[4:]
    path=(u.path or "").rstrip("/")
    qs=urllib.parse.parse_qsl(u.query, keep_blank_values=True)
    kept=tuple(sorted((k,val) for k,val in qs if k.lower() not in SKIP and not k.lower().startswith("utm_")))
    return (host, path, kept)
def differ():
    sys.stdout.write(f"{title} · {stored_id}")
    sys.exit(1)
given_id=vid_from_url(given)
url_id=vid_from_url(stored_url)
id_a=url_id or stored_id
id_b=given_id
if id_a and id_b:
    sys.exit(0) if id_a==id_b else differ()
if stored_url:
    sys.exit(0) if url_key(stored_url)==url_key(given) else differ()
sys.exit(2)' "$1" "$2"
}
if [ -f "$OUT/원본자막/meta.json" ] && meta_ok "$OUT/원본자막/meta.json" 2>/dev/null; then
  same_rc=0
  same_info="$(meta_same "$OUT/원본자막/meta.json" "$URL")" || same_rc=$?
  if [ "$same_rc" -eq 1 ]; then
    log "[오류] 이 작업 폴더에는 이미 다른 영상(${same_info})이 들어 있다."
    log "같은 폴더에 두 영상을 섞으면 글이 뒤섞인다. 다른 폴더를 쓰거나 이 폴더를 비워라"
    exit 1
  fi
  if [ "$same_rc" -eq 2 ]; then
    if [ "$SAME_OK" -eq 1 ]; then
      log "[경고] 같은 영상인지 견줄 수 없다 — --same-ok 가 있어 있는 파일로 진행한다"
    else
      log "[오류] 같은 영상인지 모르겠다 — 이 폴더에 이어 쓰려면 --same-ok 를 붙여 다시"
      exit 1
    fi
  fi
  log "영상 정보는 이미 있다 — 다시 받지 않는다"
else
  rm -f "$OUT/원본자막/meta.json"
  log "영상 정보 받는 중"
  "$YT" "${JS[@]}" "${NO_PLAYLIST[@]}" --dump-json --skip-download "$URL" > "$OUT/원본자막/meta.json"
  if ! meta_ok "$OUT/원본자막/meta.json" 2>/dev/null; then
    log "[오류] 영상 정보 파일이 JSON 으로 읽히지 않거나 id 가 없다"
    exit 1
  fi
fi
VID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$OUT/원본자막/meta.json")"
log "영상 번호 = $VID"

# 3. 받을 자막 언어 고르기 — 사람이 단 자막(ko·en·영상 언어), 원래 음성의 -orig(발화 원문),
#    ko·en 더빙의 -orig(자동 더빙 받아쓰기). 자동 번역은 이 목록에 넣지 않는다.
#    ko·en 이 아닌 더빙은 목록에 없다. 자동 번역은 이 받기가 성공한 뒤 한 번만 따로 시도한다.

# 없어졌거나 비공개면 다시 받아도 안 된다. 이번 시도 기록만 본다.
unavailable_in() {
  grep -E -i -q \
    'Private video|This video is private|Video unavailable|This video is unavailable|This video has been removed|video has been removed|removed by the uploader|no longer available|account associated with this video has been terminated|HTTP Error 404' \
    "$1"
}
# pick_langs.py 표준 출력 1줄만 먼저 받는다. 2줄은 나중 자동 번역이라 여기 넣지 않는다.
# head 로 자르면 파이프가 끊겨 종료 코드가 실패로 잡힐 수 있어, 줄바꿈 앞만 자른다.
pick_out="$(python3 "$HERE/pick_langs.py" "$OUT/원본자막/meta.json" "$OUT/원본자막/자막_출처.json")"
LANGS="${pick_out%%$'\n'*}"
MISSING=""
for L in ${LANGS//,/ }; do [ -s "$OUT/원본자막/$VID.$L.json3" ] || MISSING="$MISSING,$L"; done
MISSING="${MISSING#,}"
if [ -z "$LANGS" ]; then
  log "[경고] 이 영상에는 받을 자막이 없다 — 화면 글자 인식만 가능"
elif [ -z "$MISSING" ]; then
  log "자막은 이미 다 받아 두었다 — 다시 받지 않는다(같은 자막을 또 받으면 유튜브가 429 로 막는다, 2026-09-11 실측)"
else
  # 429(요청이 너무 많다)는 못 받는 게 아니라 잠깐 막힌 것 — 쉬었다가 두 번 더 시도한다
  try_n=0
  for WAIT in 0 30 90; do
    try_n=$((try_n+1))
    MISSING=""
    for L in ${LANGS//,/ }; do [ -s "$OUT/원본자막/$VID.$L.json3" ] || MISSING="$MISSING,$L"; done
    MISSING="${MISSING#,}"
    [ -z "$MISSING" ] && break
    [ "$WAIT" -gt 0 ] && { log "429 등으로 막혀 ${WAIT}초 쉬고 다시"; sleep "$WAIT"; }
    log "받을 자막 = $MISSING"
    echo "── ${try_n}번째 시도 $(date '+%H:%M:%S %Z') ──" >> "$OUT/원본자막/받기기록.txt"
    sub_try="$(mktemp /tmp/v2c_sub.XXXXXX)"
    sub_rc=0
    "$YT" "${JS[@]}" "${NO_PLAYLIST[@]}" --skip-download --write-subs --write-auto-subs --sleep-subtitles 3 \
        --sub-langs "$MISSING" --sub-format json3 \
        -o "$OUT/원본자막/%(id)s.%(ext)s" "$URL" > "$sub_try" 2>&1 || sub_rc=$?
    cat "$sub_try" >> "$OUT/원본자막/받기기록.txt"
    if unavailable_in "$sub_try"; then
      rm -f "$sub_try"
      log "[오류] 영상이 없거나 비공개라 다시 받아도 안 된다 — $OUT/원본자막/받기기록.txt 끝줄을 본다"
      tail -3 "$OUT/원본자막/받기기록.txt"
      exit 1
    fi
    rm -f "$sub_try"
    unset sub_rc
    MISSING=""
    for L in ${LANGS//,/ }; do [ -s "$OUT/원본자막/$VID.$L.json3" ] || MISSING="$MISSING,$L"; done
    MISSING="${MISSING#,}"
    [ -z "$MISSING" ] && break
  done
  if [ -n "$MISSING" ]; then
    log "[오류] 자막 받기 세 번 실패 — $OUT/원본자막/받기기록.txt 끝줄:"; tail -3 "$OUT/원본자막/받기기록.txt"
    host="$(python3 -c 'import sys,urllib.parse; print(urllib.parse.urlparse(sys.argv[1]).netloc.lower().removeprefix("www."))' "$URL")"
    case "$host" in
      youtube.com|m.youtube.com|music.youtube.com|youtube-nocookie.com|m.youtube-nocookie.com|youtu.be|*.youtube.com|*.youtu.be)
        log "유튜브에서 자막 받기가 세 번 막혔다. 10분 뒤 같은 명령을 다시 돌려라"
        ;;
      *)
        log "유튜브가 아닌 사이트에서 자막 받기가 세 번 막혔다. 막힌 사이트를 읽는 다른 도구가 있으면 그것으로 넘긴다"
        ;;
    esac
    exit 1
  fi
  found_json3=0
  for f in "$OUT/원본자막"/*.json3; do
    [ -f "$f" ] || continue
    found_json3=1
    echo "  받은 자막 = ${f##*/}"
  done
  if [ "$found_json3" -eq 0 ]; then
    log "[오류] 요청한 자막을 받았다고 하는데 파일이 하나도 없다"
    exit 1
  fi
fi

# 3b. 사용자 언어 자동 번역은 위가 성공한 뒤에만, 딱 한 번. 실패해도 쉬지 않고 이어 간다.
#     이름표 「자동 번역」은 파일이 생긴 뒤에만 고른_자막에 넣는다. 없으면 subs_to_md 가 없는 파일을 경고한다.
later_lang="$(python3 - "$OUT/원본자막/자막_출처.json" << 'PY' || true
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, json.JSONDecodeError, UnicodeError):
    sys.exit(0)
if not isinstance(d, dict):
    sys.exit(0)
later = d.get("나중에_자동번역") or {}
if isinstance(later, dict) and later:
    print(next(iter(later)))
PY
)"
if [ -n "$later_lang" ]; then
  if [ -s "$OUT/원본자막/$VID.$later_lang.json3" ]; then
    log "자동 번역 자막은 이미 받아 두었다 — 다시 받지 않는다"
  else
    log "자동 번역 자막 한 번 시도 = $later_lang"
    echo "── 자동번역 1번째 시도 $(date '+%H:%M:%S %Z') ──" >> "$OUT/원본자막/받기기록.txt"
    sub_try="$(mktemp /tmp/v2c_sub.XXXXXX)"
    sub_rc=0
    "$YT" "${JS[@]}" "${NO_PLAYLIST[@]}" --skip-download --write-subs --write-auto-subs --sleep-subtitles 3 \
        --sub-langs "$later_lang" --sub-format json3 \
        -o "$OUT/원본자막/%(id)s.%(ext)s" "$URL" > "$sub_try" 2>&1 || sub_rc=$?
    cat "$sub_try" >> "$OUT/원본자막/받기기록.txt"
    rm -f "$sub_try"
    unset sub_rc
    if [ -s "$OUT/원본자막/$VID.$later_lang.json3" ]; then
      python3 - "$OUT/원본자막/자막_출처.json" "$later_lang" << 'PY'
import json, sys
path, lang = sys.argv[1], sys.argv[2]
d = json.load(open(path, encoding="utf-8"))
later = d.get("나중에_자동번역") or {}
kind = later.get(lang) if isinstance(later, dict) else None
d.setdefault("고른_자막", {})[lang] = kind or "자동 번역"
json.dump(d, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
      log "자동 번역 자막을 받았다 = $later_lang"
    else
      log "자동 번역 자막은 못 받음 — 원문 자막으로 진행, 번역은 클로드가 한다"
    fi
  fi
fi

# 4. 글자 인식용 영상 파일(요청했을 때만). 소리는 필요 없어 영상만 받는다.
if [ "$WANT_VIDEO" -eq 1 ]; then
  mkdir -p "$OUT/원본영상"
  have_video=0
  for f in "$OUT/원본영상/${VID}_720p".*; do
    [ -f "$f" ] || continue
    case "${f##*/}" in
      *받기기록*) continue ;;
    esac
    have_video=1
    break
  done
  if [ "$have_video" -eq 1 ]; then
    log "영상 파일은 이미 있다 — 다시 받지 않는다"
  else
    OK=""
    try_n=0
    for WAIT in 0 30 90; do
      try_n=$((try_n+1))
      [ "$WAIT" -gt 0 ] && { log "막혀서 ${WAIT}초 쉬고 다시"; sleep "$WAIT"; }
      log "720p 영상 받는 중"
      # H.264(avc1) 를 먼저 고른다 — AV1 은 이 컴퓨터의 OpenCV 가 못 푼다(2026-09-11 실측 = 장면 0장)
      echo "── ${try_n}번째 시도 $(date '+%H:%M:%S %Z') ──" >> "$OUT/원본영상/받기기록.txt"
      vid_try="$(mktemp /tmp/v2c_vid.XXXXXX)"
      if "$YT" "${JS[@]}" "${NO_PLAYLIST[@]}" -f "bv*[height<=720][vcodec^=avc1]/bv*[height<=720][vcodec!^=av01]/b[height<=720][vcodec!^=av01]" --no-part \
          -o "$OUT/원본영상/${VID}_720p.%(ext)s" "$URL" > "$vid_try" 2>&1; then
        cat "$vid_try" >> "$OUT/원본영상/받기기록.txt"
        rm -f "$vid_try"
        OK=1; break
      fi
      cat "$vid_try" >> "$OUT/원본영상/받기기록.txt"
      if unavailable_in "$vid_try"; then
        rm -f "$vid_try"
        log "[오류] 영상이 없거나 비공개라 다시 받아도 안 된다 — $OUT/원본영상/받기기록.txt 끝줄을 본다"
        tail -3 "$OUT/원본영상/받기기록.txt"
        exit 1
      fi
      rm -f "$vid_try"
    done
    [ -n "$OK" ] || { log "[오류] 영상 받기 세 번 실패 — $OUT/원본영상/받기기록.txt 끝줄:"; tail -3 "$OUT/원본영상/받기기록.txt"; exit 1; }
  fi
  ls -la "$OUT/원본영상"/"${VID}"_720p.* | grep -v 받기기록 | sed 's#^#  #'
fi
log "끝"
