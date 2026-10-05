# 여러 프로그램이 함께 쓰는 규칙을 한곳에 모은 파일 — 시각 표기·자막 종류 이름·챕터 고르기·되풀이 줄 찾기
# 이 파일은 cv2 를 불러오지 않는다(화면 글자 인식 없이도 자막만 다루는 프로그램이 불러 쓸 수 있어야 한다)
import sys
from collections import Counter

# ── 자막 종류 이름 — pick_langs.py 가 적고 subs_to_md.py 가 읽는다. 한 곳에서만 바꾼다
KIND_MANUAL = "사람 자막"
KIND_AUTO_ORIG = "자동 자막(발화 원문)"
KIND_AUTO_TRANS = "자동 번역"
KIND_ORDER = {KIND_MANUAL: 0, KIND_AUTO_ORIG: 1, KIND_AUTO_TRANS: 2}

# ── 화면 글자 항목을 문서에서 셀 때 쓰는 표시. 자막 글자에 우연히 "(화면)" 이 들어 있어도
#    잘못 세지 않도록, 사람이 쓸 일 없는 표시를 앞에 붙인다(문제 13)
OCR_MARK = "⟦화면글자⟧"


def fmt(total_seconds):
    """초를 시:분:초 또는 분:초 글자로 바꾼다. 모든 프로그램이 이 함수만 쓴다."""
    s = max(0, int(total_seconds))
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def escape_heading(line):
    """설명란의 해시태그 줄이 md 제목으로 읽히지 않게 앞에 역슬래시를 붙인다."""
    return "\\" + line if line.lstrip().startswith("#") else line


def normalize_chapters(chapters, duration):
    """첫 챕터가 0초보다 늦게 시작하면 그 앞 구간을 '시작 부분' 챕터로 채운다.
    (문제 27 — 예전에는 첫 챕터 시작 전 자막이 조용히 첫 챕터로 들어갔다)"""
    chapters = [dict(c) for c in (chapters or [])]
    if not chapters:
        return [{"start_time": 0, "end_time": duration or 0, "title": "전체"}]
    if chapters[0]["start_time"] > 0:
        sys.stderr.write(
            f"[알림] 첫 챕터가 {fmt(chapters[0]['start_time'])} 에 시작한다 — "
            f"그 앞 구간을 '시작 부분' 챕터로 따로 둔다\n"
        )
        chapters.insert(0, {"start_time": 0, "end_time": chapters[0]["start_time"], "title": "시작 부분"})
    return chapters


def chapter_of(t_sec, chapters):
    """그 시각이 어느 챕터에 드는지 번호를 돌려준다. normalize_chapters 를 거친 목록을 받는다."""
    for i, ch in enumerate(chapters):
        last = i == len(chapters) - 1
        if t_sec >= ch["start_time"] and (last or t_sec < ch["end_time"]):
            return i
    return 0


def repeated_lines(items, min_screens=6, min_share=0.5, min_len=3):
    """거의 모든 화면에 되풀이되는 줄(채널 이름·로고 등)을 찾아 돌려준다.
    ⚠ 찾기만 한다 — 빼는 것은 이 함수의 몫이 아니다(문제 14·15 — 예전에는 여기서 찾은 줄을
    본문에서 빼 버렸는데, 실측해 보니 걸리는 것은 로고가 아니라 '+'·'C' 같은 글자 인식 오류 조각이었다).
    글자 수가 min_len 보다 짧은 줄은 인식 오류일 가능성이 커서 아예 세지 않는다."""
    screens = [it for it in items if it["region"] == "화면" and it["text"]]
    if len(screens) < min_screens:
        return set()
    counts = Counter(
        line for it in screens for line in set(it["text"].split("\n")) if len(line.strip()) >= min_len
    )
    return {l for l, c in counts.items() if c >= min_share * len(screens)}

def chapters_of(start, end, chapters):
    """한 항목이 걸친 챕터 번호를 전부 돌려준다(화면 글자는 여러 챕터에 걸칠 수 있다).
    ⚠ 끝 시각이 그 챕터의 시작 시각과 딱 같으면 실제로는 그 챕터에 안 걸친 것이므로 뺀다
      (2026-09-13 실측 = 안 빼면 340초·420초 항목이 다음 챕터에 잘못 들어간다).
    ⚠ 시작과 끝이 같은 항목(화면이 한 번만 잡힌 것)도 한 챕터를 돌려준다 — 시각 범위가 겹치는지로
      따지면 그런 항목 6개가 통째로 사라진다(같은 날 실측)."""
    i0 = chapter_of(start, chapters)
    i1 = chapter_of(end, chapters)
    if i1 > i0 and end == chapters[i1]["start_time"]:
        i1 -= 1
    return list(range(i0, max(i0, i1) + 1))
