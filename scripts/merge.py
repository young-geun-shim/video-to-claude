# 자막(자막_줄.json)과 화면 글자 인식 결과(화면글자_OCR_원본.json, 있으면)를 챕터별로 합쳐 영상_전체텍스트.md 를 만들고 줄 수를 검산한다
# 같이 읽기용.md 를 만든다. 원래 음성 언어 자막 한 벌(없으면 화면 글자)과 화면 글자를 시각 순으로 섞는다.
# 쓰는 법: python3 merge.py <작업 폴더> [--from 초 --to 초 --suffix _1부]
#   --to 에 적은 시각은 범위에 넣지 않는다(t >= --to 이면 빼고, kept_sec·kept_span 과 같다).
#   인자를 안 주면 예전과 같이 영상 전체를 영상_전체텍스트.md 와 읽기용.md 로 쓴다.
import difflib
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    KIND_AUTO_ORIG,
    KIND_AUTO_TRANS,
    KIND_MANUAL,
    OCR_MARK,
    chapter_of,
    chapters_of,
    escape_heading,
    fmt,
    repeated_lines,
)
from pick_langs import KIND_DUB, KIND_SPEECH, base, original_speech_bases  # noqa: E402
from subs_to_md import die_unreadable_subs, duration_lines, load_rows, safe_title  # noqa: E402


def parse_merge_args(argv):
    folder = None
    from_s = None
    to_s = None
    suffix = ""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--from":
            i += 1
            if i >= len(argv):
                sys.exit("[오류] --from 다음에 초가 없다")
            from_s = float(argv[i])
        elif a == "--to":
            i += 1
            if i >= len(argv):
                sys.exit("[오류] --to 다음에 초가 없다")
            to_s = float(argv[i])
        elif a == "--suffix":
            i += 1
            if i >= len(argv):
                sys.exit("[오류] --suffix 다음에 글자가 없다")
            suffix = argv[i]
        elif a.startswith("--"):
            sys.exit(f"[오류] 모르는 인자 {a}")
        else:
            if folder is not None:
                sys.exit("[오류] 작업 폴더를 두 번 받았다")
            folder = a
        i += 1
    if not folder:
        sys.exit(
            "[오류] 작업 폴더가 없다. 쓰는 법 = python3 merge.py <작업 폴더> "
            "[--from 초 --to 초 --suffix _1부]  (--to 시각 자체는 포함 안 됨)"
        )
    if any(ch in suffix for ch in "/\\") or suffix.startswith("."):
        sys.exit("[오류] --suffix 는 파일 이름 뒤에 붙는 글자만 받는다")
    if from_s is not None and to_s is not None and to_s < from_s:
        sys.exit("[오류] --to 가 --from 보다 이르다")
    return Path(folder), from_s, to_s, suffix


READING_NEAR_SEC = 3.0
READING_SAME_LINE = 0.85


def _norm_line(s):
    return re.sub(r"\s+", "", s).lower()


def _meaningful(s):
    return len(re.findall(r"[0-9A-Za-z가-힣]", s)) >= 2


def _same_line(a, b, ratio):
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= ratio


def ocr_shown_for_reading(out, fallback_shown):
    """읽기용만 — 구간_본.json 읽기에서 같은 줄 후보를 모아 가장 많이 나온 글을 고른다. 원본 OCR json 은 안 건드린다."""
    path = out / "분석" / "구간_본.json"
    if not path.is_file():
        return fallback_shown
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return fallback_shown
    reads = data.get("reads")
    if not isinstance(reads, list) or not reads:
        return fallback_shown
    try:
        ratio = float((data.get("settings") or {}).get("same_line") or READING_SAME_LINE)
    except (TypeError, ValueError):
        ratio = READING_SAME_LINE
    obs = []
    for r in reads:
        region = r.get("region")
        if region not in ("화면", "자막띠"):
            continue
        t = float(r["t"])
        lines = r.get("lines") or []
        scores = r.get("rec_scores") or []
        if len(scores) != len(lines):
            scores = [None] * len(lines)
        for line, sc in zip(lines, scores):
            if not _meaningful(line):
                continue
            obs.append((t, region, line, sc))
    if not obs:
        return fallback_shown
    obs.sort(key=lambda x: (x[1], x[0]))
    clusters = []
    for t, region, line, sc in obs:
        n = _norm_line(line)
        placed = False
        for cl in clusters:
            if cl["region"] != region or abs(t - cl["t_ref"]) > READING_NEAR_SEC:
                continue
            if not _same_line(n, cl["norm_ref"], ratio):
                continue
            cl["ts"].append(t)
            cl["votes"][line] += 1
            prev = cl["best_score"].get(line)
            if sc is not None and (prev is None or sc > prev):
                cl["best_score"][line] = float(sc)
            placed = True
            break
        if not placed:
            clusters.append(
                {
                    "region": region,
                    "t_ref": t,
                    "norm_ref": n,
                    "ts": [t],
                    "votes": Counter({line: 1}),
                    "best_score": {line: float(sc)} if sc is not None else {},
                }
            )

    def pick_text(cl):
        def rank(text):
            cnt = cl["votes"][text]
            sc = cl["best_score"].get(text)
            return (cnt, sc if sc is not None else -1.0, text)

        return max(cl["votes"].keys(), key=rank)

    items = []
    for cl in clusters:
        text = pick_text(cl)
        if not text.strip():
            continue
        start, end = min(cl["ts"]), max(cl["ts"])
        items.append(
            {
                "region": cl["region"],
                "start": round(start, 1),
                "end": round(end, 1),
                "text": text,
            }
        )
    items.sort(key=lambda it: (it["start"], it["region"]))
    return items if items else fallback_shown


def kept_sec(t, from_s, to_s):
    if from_s is not None and t < from_s:
        return False
    if to_s is not None and t >= to_s:
        return False
    return True


def kept_span(a0, a1, from_s, to_s):
    if a1 < a0:
        a1 = a0
    lo = from_s if from_s is not None else float("-inf")
    hi = to_s if to_s is not None else float("inf")
    if a0 == a1:
        return lo <= a0 < hi
    return a0 < hi and a1 > lo


def choose_speech_track(tracks, speech):
    """원래 음성 언어 자막 한 벌. 사람 자막을 먼저, 그 다음 발화 원문. 더빙과 자동 번역은 넣지 않는다."""
    cands = []
    for t in tracks:
        if base(t.get("lang")) not in speech:
            continue
        if t.get("kind") in (KIND_DUB, KIND_AUTO_TRANS):
            continue
        cands.append(t)
    if not cands:
        return None

    def rank(t):
        if t.get("kind") == KIND_MANUAL:
            return 0
        if str(t.get("lang") or "").endswith("-orig") or t.get("kind") in (KIND_SPEECH, KIND_AUTO_ORIG):
            return 1
        return 2

    cands.sort(key=rank)
    return cands[0]


def speech_label(track):
    if track.get("kind") == KIND_MANUAL:
        return KIND_MANUAL
    if track.get("kind") in (KIND_SPEECH, KIND_AUTO_ORIG) or str(track.get("lang") or "").endswith("-orig"):
        return KIND_SPEECH
    return track.get("kind") or ""


def write_reading(path, meta, tracks, shown, ocr_missing, from_s, to_s):
    speech = original_speech_bases(meta)
    track = choose_speech_track(tracks, speech)
    names = ",".join(sorted(speech)) or "모름"
    events = []
    if track is None:
        sys.stderr.write(f"[경고] 원래 음성 언어({names}) 자막이 없어 읽기용은 화면 글자만 담는다\n")
    else:
        label = speech_label(track)
        for ms, text in track["rows"]:
            sec = ms / 1000
            if not kept_sec(sec, from_s, to_s):
                continue
            events.append((sec, 0, "sub", text, label, track["lang"]))
    if not ocr_missing:
        for it in shown:
            if not kept_span(it["start"], it["end"], from_s, to_s):
                continue
            events.append((float(it["start"]), 1, "ocr", it, "", ""))
    events.sort(key=lambda e: (e[0], e[1]))

    up = meta.get("upload_date") or ""
    lines = [
        f"# {safe_title(meta.get('title'))}",
        "",
        f"- 채널: {meta.get('channel') or meta.get('uploader') or ''}",
        f"- URL: {meta.get('webpage_url') or ''}",
        f"- 올린 날짜: {up[:4]}-{up[4:6]}-{up[6:8]}" if up else "- 올린 날짜: (없음)",
        *duration_lines(meta),
        "- 읽기용은 원래 음성 언어 자막 한 벌과 화면 글자를 시각 순으로 섞은 것이다. 자막이 없으면 화면 글자만 담는다.",
        f"- 원래 음성 언어 = {names}",
    ]
    if from_s is not None or to_s is not None:
        lo_txt = f"{from_s:g}초" if from_s is not None else "처음"
        hi_txt = f"{to_s:g}초" if to_s is not None else "끝"
        lines.append(f"- 부분 범위 = {lo_txt} ~ {hi_txt} (--to 시각 자체는 포함 안 됨)")
    if track is None:
        lines.append("- 담은 자막 = 없음")
    else:
        lines.append(f"- 담은 자막 = {track['lang']} ({speech_label(track)})")
    if ocr_missing:
        lines.append("- 화면 글자 인식은 아직 없다.")
    else:
        lines.append("- 화면 글자 인식은 틀릴 수 있다. 자막띠는 화면에 박힌 자막이고 화면은 슬라이드 등 나머지 글자다.")
    lines.append("- 같은 시각이면 자막 줄을 화면 글자보다 앞에 둔다.")
    lines.append("")
    if not events:
        lines.append("⚠ 이 범위에 담을 자막도 화면 글자도 없다")
        lines.append("")
    for ev in events:
        if ev[2] == "sub":
            sec, _prio, _kind, text, label, lang = ev
            lines.append(f"[{fmt(sec)}] ({lang} {label}) {text}")
        else:
            it = ev[3]
            lines.append(f"[{fmt(it['start'])}~{fmt(it['end'])}] ({'화면에 박힌 자막' if it['region']=='자막띠' else '화면 글자'})")
            lines.extend(escape_heading(l) for l in it["text"].split("\n"))
            lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return track is not None, len(events)


def main():
    out, from_s, to_s, suffix = parse_merge_args(sys.argv[1:])
    meta = json.loads((out / "원본자막" / "meta.json").read_text(encoding="utf-8"))
    subs = json.loads((out / "자막_줄.json").read_text(encoding="utf-8"))
    chapters, tracks = subs["chapters"], subs["tracks"]
    ocr_path = out / "화면글자_OCR_원본.json"
    ocr_items = json.loads(ocr_path.read_text(encoding="utf-8")) if ocr_path.exists() else None

    repeated = set()
    shown = []
    if ocr_items is not None:
        # 되풀이되는 줄은 머리말에 목록으로 적기만 하고 본문에서는 빼지 않는다(원칙 = 1개도 빼먹지 않는다)
        repeated = repeated_lines(ocr_items)
        for it in ocr_items:
            if it["text"].strip():
                shown.append(dict(it))

    empty_warn = "⚠ 옮긴 자막이 하나도 없다 — 말한 내용은 이 파일에 없다"
    up = meta.get("upload_date") or ""
    lines = [
        f"# {safe_title(meta.get('title'))}",
        "",
        f"- 채널: {meta.get('channel') or meta.get('uploader') or ''}",
        f"- URL: {meta.get('webpage_url') or ''}",
        f"- 올린 날짜: {up[:4]}-{up[4:6]}-{up[6:8]}" if up else "- 올린 날짜: (없음)",
        *duration_lines(meta),
        "",
        "## 이 문서를 읽는 법",
        "",
    ]
    if tracks:
        lines.append("- 챕터마다 자막(" + " · ".join(f"{t['lang']} = {t['kind']}" for t in tracks) + ")을 먼저, 화면 글자 인식 결과를 뒤에 둔다.")
        lines.append("- 자막 줄은 원본 자막 파일과 한 줄도 다르지 않다(자막_전문.md 의 검산 참고).")
    else:
        lines.append("- 이 영상에는 받을 수 있는 자막이 없어 말한 내용은 옮기지 못했다. 화면 글자 인식 결과만 뒤에 둔다.")
        lines.append("- " + empty_warn)
    if ocr_items is None:
        lines.append("- ⚠ 화면 글자 인식은 아직 합치지 않았다. 끝나면 merge.py 를 다시 돌린다.")
    else:
        lines.append("- 화면 글자 인식은 틀릴 수 있다. (자막띠) = 화면에 박힌 자막, (화면) = 슬라이드·도표 등 나머지 화면 글자.")
        if repeated:
            lines.append("- 거의 모든 화면에 되풀이된 글자(본문에서는 빼지 않았다) = " + " / ".join(sorted(repeated)))
    if from_s is not None or to_s is not None:
        lo_txt = f"{from_s:g}초" if from_s is not None else "처음"
        hi_txt = f"{to_s:g}초" if to_s is not None else "끝"
        lines.append(
            f"- 부분 범위 = {lo_txt} ~ {hi_txt} (이 범위 밖 줄은 이 파일에 없다, --to 시각 자체는 포함 안 됨)"
        )
    lines += ["", "## 영상 설명란", ""]
    lines += [escape_heading(l) for l in (meta.get("description") or "(없음)").splitlines()]
    lines += ["", "## 챕터 표", "", "시작시각 | 제목", "---|---"]
    lines += [f"{fmt(c['start_time'])} | {safe_title(c.get('title'))}" for c in chapters]
    lines.append("")

    for ci, ch in enumerate(chapters):
        lines += [f"## {fmt(ch['start_time'])} {safe_title(ch.get('title'))}", ""]
        for t in tracks:
            lines += [f"### 자막 — {safe_title(t['lang'])} ({safe_title(t['kind'])})", ""]
            lines += [
                f"[{fmt(ms // 1000)}] {text}"
                for ms, text in t["rows"]
                if chapter_of(ms / 1000, chapters) == ci and kept_sec(ms / 1000, from_s, to_s)
            ]
            lines.append("")
        if ocr_items is not None:
            lines += ["### 화면 글자 인식", ""]
            for it in shown:
                if ci in chapters_of(it["start"], it["end"], chapters) and kept_span(
                    it["start"], it["end"], from_s, to_s
                ):
                    lines += [f"{OCR_MARK}[{fmt(it['start'])}~{fmt(it['end'])}] ({it['region']})"]
                    lines += [escape_heading(l) for l in it["text"].split("\n")]
                    lines.append("")

    # 검산 = 고른 자막 파일이 있는지, 버린 줄에 글자가 있었는지, 원본 줄 수와 이 문서의 줄 수가 같은지
    body = "\n".join(lines)
    problems, table = [], []
    src = out / "원본자막"
    picked = json.loads((src / "자막_출처.json").read_text(encoding="utf-8")).get("고른_자막") or {}
    vid = meta["id"]
    track_by_lang = {t["lang"]: t for t in tracks}
    for lang, kind in picked.items():
        path = src / f"{vid}.{lang}.json3"
        if not path.exists():
            table.append(f"자막 {lang} ({kind}) | 파일 없음 | ")
            problems.append(f"자막 {lang} 파일 없음")
            continue
        try:
            src_rows_all, dropped = load_rows(path)
            src_rows = [row for row in src_rows_all if kept_sec(row[0] / 1000, from_s, to_s)]
        except Exception as e:
            if isinstance(e, SystemExit):
                raise
            die_unreadable_subs(path, str(e))
        if dropped:
            for t_ms, text in dropped:
                sys.stderr.write(
                    f"[오류] {lang} aAppend=1 인데 글자가 있다 — {fmt(t_ms // 1000)} = {text}\n"
                )
            problems.append(f"자막 {lang} aAppend 인데 글자 있던 줄 {len(dropped)}개")
        t = track_by_lang.get(lang)
        head = f"### 자막 — {safe_title(lang)} ({safe_title(kind)})"
        n_md, inside = 0, False
        for line in body.splitlines():
            if line.startswith("### "):
                inside = line == head
            elif line.startswith("## "):
                inside = False
            elif inside and line.startswith("[") and "] " in line:
                n_md += 1
        table.append(f"자막 {lang} ({kind}) | {len(src_rows)} | {n_md}")
        if n_md != len(src_rows):
            problems.append(f"자막 {lang} 원본 {len(src_rows)} · 문서 {n_md}")
        if t is not None:
            track_rows = [row for row in t["rows"] if kept_sec(row[0] / 1000, from_s, to_s)]
            if len(track_rows) != len(src_rows):
                problems.append(f"자막 {lang} 원본 {len(src_rows)} · 자막_줄.json {len(track_rows)}")
    if ocr_items is not None:
        shown_kept = [it for it in shown if kept_span(it["start"], it["end"], from_s, to_s)]
        n_orig = len(ocr_items)
        n_shown = len(shown_kept)
        n_expected = sum(len(chapters_of(it["start"], it["end"], chapters)) for it in shown_kept)
        n_md = sum(1 for l in body.splitlines() if l.startswith(OCR_MARK))
        empty_n = n_orig - n_shown
        empty_note = f" 빈 항목 {empty_n}개" if empty_n else ""
        table.append(f"화면 글자 인식 항목 | {n_orig} | {n_shown} | {n_expected} | {n_md}{empty_note}")
        if n_md != n_expected:
            problems.append(f"화면 글자 찍혀야 할 수 {n_expected} · 문서 {n_md}")
    lines += ["## 검산", "", "종류 | 원본 | 이 문서", "---|---|---"] + table + [""]
    full_path = out / f"영상_전체텍스트{suffix}.md"
    read_path = out / f"읽기용{suffix}.md"
    full_path.write_text("\n".join(lines), encoding="utf-8")
    reading_shown = ocr_shown_for_reading(out, shown) if ocr_items is not None else shown
    write_reading(read_path, meta, tracks, reading_shown, ocr_items is None, from_s, to_s)
    for row in table:
        print("검산 " + row)
    print(f"읽기용 = {read_path.name}")
    if problems:
        sys.stderr.write("[오류] 검산 어긋남 — " + " / ".join(problems) + "\n")
        sys.exit(1)
    if not tracks:
        if ocr_items is not None:
            print(f"검산 통과 — 화면 글자만(자막 없음) — {full_path.name}")
        else:
            print(empty_warn)
        return
    print(
        "검산 통과 — "
        + full_path.name
        + (" (화면 글자 인식 포함)" if ocr_items is not None else " (자막만, 화면 글자 인식 전)")
    )


if __name__ == "__main__":
    main()
