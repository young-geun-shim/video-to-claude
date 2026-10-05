# 영상 정보(meta.json)를 읽어 받을 자막 언어를 고르고, 언어마다 사람 자막인지 자동 자막인지 적어 둔다
# 쓰는 법: python3 pick_langs.py <meta.json> <자막_출처.json>
#   표준 출력 1줄 = 먼저 받을 언어(쉼표). 2줄 = 나중에 한 번만 받을 자동 번역(없으면 빈 줄).
# 원래 음성 언어는 formats 오디오 트랙 이름(예 Korean original (default))에서 찾는다.
# 그 언어의 -orig 는 발화 원문, 다른 언어의 -orig 는 자동 더빙 받아쓰기다. ko·en 이 아닌 더빙은 받지 않는다.
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import KIND_AUTO_TRANS, KIND_MANUAL  # noqa: E402

# 이름 문자열. common.py 의 「자동 자막(발화 원문)」은 더빙까지 한 이름으로 부르던 옛 이름이다.
KIND_SPEECH = "발화 원문"
KIND_DUB = "자동 더빙 받아쓰기"
DUB_KEEP = {"ko", "en"}
# 사용자가 읽는 언어. 환경 변수 VIDEO_TO_CLAUDE_LANG(앞뒤 공백 제거·소문자)이 있으면 그 값, 없으면 ko.
_lang = (os.environ.get("VIDEO_TO_CLAUDE_LANG") or "").strip().lower()
USER_LANG = _lang if _lang else "ko"


def base(code):
    return (code or "").split("-")[0].lower()


def _fallback_speech(meta):
    """오디오 트랙에서 못 찾으면 영상 정보 언어를 쓰고, 그것도 없으면 빈 집합. 어느 쪽이든 경고한다."""
    lang = base(meta.get("language"))
    if lang:
        sys.stderr.write(f"[경고] 영상 정보 언어({lang})를 원래 음성 언어로 쓴다\n")
        return {lang}
    sys.stderr.write("[경고] 원래 음성 언어를 정하지 못했다\n")
    return set()


def original_speech_bases(meta):
    """formats 의 오디오 트랙 가운데 이름에 original 이 있는 언어 코드(ko 처럼 앞부분)를 돌려준다.
    (default) 가 있으면 그 언어만. formats 가 없거나 자막 판단에 실패해도 예외를 내지 않는다."""
    formats = meta.get("formats") if isinstance(meta, dict) else None
    if not isinstance(formats, list) or not formats:
        sys.stderr.write("[경고] meta.json 에 formats 가 없어 오디오 트랙 이름에서 원래 음성 언어를 못 찾는다\n")
        return _fallback_speech(meta if isinstance(meta, dict) else {})
    found = []
    for fmt in formats:
        if not isinstance(fmt, dict):
            continue
        ac = fmt.get("acodec")
        vc = fmt.get("vcodec")
        if not ac or ac == "none":
            continue
        if vc not in (None, "none"):
            continue
        note = str(fmt.get("format_note") or "")
        if "original" not in note.lower():
            continue
        lang = base(fmt.get("language"))
        if not lang:
            sys.stderr.write(f"[경고] 원래 음성으로 보이는 오디오 트랙({note})에 언어 코드가 없다\n")
            continue
        found.append((lang, "default" in note.lower()))
    if not found:
        sys.stderr.write("[경고] formats 오디오 트랙 이름에서 원래 음성을 못 찾았다\n")
        return _fallback_speech(meta)
    defaults = {lang for lang, is_default in found if is_default}
    return defaults or {lang for lang, _is_default in found}


def later_auto_trans(picked, manual, auto, user=USER_LANG):
    """먼저 받는 목록이 성공한 뒤 한 번만 받을 자동 번역. 사람 자막이 그 언어에 있으면 빈 딕셔너리.
    자동 자막이 하나도 없으면 번역할 원문이 없으므로 빈 딕셔너리. 그 언어 코드가 먼저 받는 목록에 있으면 또 받지 않는다."""
    if not isinstance(auto, dict) or not auto:
        return {}
    if not isinstance(manual, dict):
        manual = {}
    if any(base(c) == user for c in manual):
        return {}
    if user in picked:
        return {}
    return {user: KIND_AUTO_TRANS}


def main():
    out_path = sys.argv[2]
    try:
        meta = json.load(open(sys.argv[1], encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as e:
        sys.stderr.write(f"[경고] 영상 정보를 읽지 못했다 ({e})\n")
        meta = {}
    if not isinstance(meta, dict):
        sys.stderr.write("[경고] 영상 정보가 객체가 아니라 빈 정보로 본다\n")
        meta = {}
    manual = {k: v for k, v in (meta.get("subtitles") or {}).items() if k != "live_chat"}
    auto = meta.get("automatic_captions") or {}
    if not isinstance(auto, dict):
        sys.stderr.write("[경고] automatic_captions 가 객체가 아니라 자막 목록을 못 읽는다\n")
        auto = {}
    video_lang = base(meta.get("language"))
    speech = original_speech_bases(meta)

    if not manual and not auto:
        sys.stderr.write("[경고] 이 영상 정보에 자막이 하나도 없다\n")

    picked = {}
    # 사람이 단 자막 — 한국어·영어·영상 언어·원래 음성 언어만(수십 개 언어가 달린 영상이 있다)
    keep_manual = {"ko", "en", video_lang} | speech
    for code in manual:
        if base(code) in keep_manual:
            picked[code] = KIND_MANUAL

    orig_codes = [code for code in auto if str(code).endswith("-orig")]
    if orig_codes:
        speech_hits = []
        for code in orig_codes:
            b = base(code)
            if b in speech:
                picked.setdefault(code, KIND_SPEECH)
                speech_hits.append(code)
            elif b in DUB_KEEP:
                picked.setdefault(code, KIND_DUB)
                names = ",".join(sorted(speech)) or "모름"
                sys.stderr.write(f"[알림] {code} = {KIND_DUB} (원래 음성 언어 = {names})\n")
            else:
                sys.stderr.write(f"[알림] {code} = {KIND_DUB} — ko·en 이 아니라 받지 않는다\n")
        if speech and not speech_hits:
            names = ",".join(sorted(speech))
            sys.stderr.write(
                f"[경고] 원래 음성 언어({names})의 받아쓰기(-orig)가 없다. 화면 글자 인식이 필요하다\n"
            )
    else:
        # -orig 가 없는 옛 영상. 원래 음성 언어(없으면 영상 언어)의 자동 자막을 발화 원문으로 본다.
        sys.stderr.write("[경고] -orig 자막이 없다 — 원래 음성 언어의 자동 자막을 발화 원문으로 본다\n")
        want = speech or ({video_lang} if video_lang else set())
        if want:
            for code in auto:
                if base(code) in want:
                    picked.setdefault(code, KIND_SPEECH)
        else:
            for code in auto:
                if base(code) != "ko":
                    picked.setdefault(code, KIND_SPEECH)
                    break

    # 자동 번역은 먼저 받는 목록에 넣지 않는다. 429 가 그 자막에서 난다(yt-dlp 이슈 13831).
    # 사람 자막이 사용자 언어에 없을 때만 나중 한 번 시도로 남긴다. 이름표는 「자동 번역」.
    later = later_auto_trans(picked, manual, auto)

    json.dump(
        {
            "영상_언어": meta.get("language"),
            "원래_음성_언어": sorted(speech),
            "고른_자막": picked,
            "나중에_자동번역": later,
        },
        open(out_path, "w", encoding="utf-8"),
        ensure_ascii=False,
        indent=2,
    )
    print(",".join(picked))
    print(",".join(later))


if __name__ == "__main__":
    main()
