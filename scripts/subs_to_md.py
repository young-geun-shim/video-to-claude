# 받은 자막 json3 를 빠짐없이 md 로 옮기고, 자막마다 원본 줄 수·글자와 md 가 똑같은지 검산한다
# 쓰는 법: python3 subs_to_md.py <작업 폴더>  → 자막_전문.md · 자막_줄.json. 검산이 어긋나면 종료 코드 1
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    KIND_AUTO_ORIG,
    KIND_AUTO_TRANS,
    KIND_MANUAL,
    KIND_ORDER,
    chapter_of,
    escape_heading,
    fmt,
    normalize_chapters,
)

# 글자 값은 common.py 와 같아야 한다(문제 19)
assert KIND_ORDER == {KIND_MANUAL: 0, KIND_AUTO_ORIG: 1, KIND_AUTO_TRANS: 2}

# pick_langs.py 가 적는 이름. 발화 원문은 원래 음성의 -orig, 자동 더빙 받아쓰기는 다른 언어의 -orig.
# common.py 의 옛 이름 「자동 자막(발화 원문)」도 같은 자리(1)에 둔다. 더빙은 그 다음, 자동 번역은 그 다음.
from pick_langs import KIND_DUB, KIND_SPEECH  # noqa: E402


def kind_rank(kind):
    if kind in (KIND_SPEECH, KIND_AUTO_ORIG):
        return 1
    if kind == KIND_DUB:
        return 2
    if kind == KIND_AUTO_TRANS:
        return 3
    return KIND_ORDER.get(kind, 9)


def safe_title(text):
    """제목 글자 칸만 다듬는다. 줄바꿈은 빈칸, null/빈 글자는 (제목 없음), 줄 앞 # 는 escape_heading.
    우리가 만드는 '## '/'### ' 접두에는 쓰지 않는다(문제 14·18)."""
    if text is None:
        return "(제목 없음)"
    s = str(text).replace("\r\n", " ").replace("\n", " ").replace("\r", " ").strip()
    if not s:
        return "(제목 없음)"
    return escape_heading(s)


def duration_lines(meta):
    """길이 칸이 없거나 null 이면 안내 문구. is_live 이면 경고 한 줄 더(문제 19)."""
    duration = meta.get("duration")
    lines = []
    if duration is None:
        lines.append("- 길이: (영상 정보에 길이가 없다)")
    else:
        lines.append(f"- 길이: {fmt(duration)}")
    if meta.get("is_live"):
        lines.append("- ⚠ 진행 중인 실시간 방송이다 — 자막이 일부만 있을 수 있다")
    return lines


def die_unreadable_subs(path, reason):
    """자막 json3 을 못 읽으면 안내를 찍고 종료 코드 1(문제 16)."""
    sys.stderr.write(
        f"[오류] 자막 파일을 읽을 수 없다 = {Path(path).name} ({reason}). "
        "그 파일을 지우고 fetch.sh 를 다시 돌려라\n"
    )
    sys.exit(1)


def warn_other_video_subs(src, vid, n_ours):
    """원본자막 폴더의 json3 가 지금 영상 번호의 것인지 본다(문제 4).
    다른 번호가 있으면 경고. 고른 자막을 하나도 못 읽었고 다른 번호만 있으면 종료 코드 1."""
    other_counts = {}
    for p in sorted(src.glob("*.json3")):
        if p.name.startswith(f"{vid}.") and p.name.endswith(".json3"):
            continue
        stem = p.name[: -len(".json3")] if p.name.endswith(".json3") else p.name
        other_id = stem.split(".")[0]
        other_counts[other_id] = other_counts.get(other_id, 0) + 1
    for other_id, n in other_counts.items():
        sys.stderr.write(
            f"[경고] 이 폴더에 다른 영상({other_id})의 자막 파일이 {n}개 섞여 있다\n"
        )
    if other_counts and n_ours == 0:
        sys.exit(1)


def event_text(event):
    return "".join(seg.get("utf8") or "" for seg in (event.get("segs") or []))


def load_rows(path):
    """글자가 있는 자막 줄만 (시작 ms, 글자). 줄바꿈은 빈칸 하나로 바꾼다(md 한 줄에 담기 위해).
    두 번째 값 = aAppend=1 로 버린 줄 가운데 글자가 있던 것 (시각 ms, 글자). 건너뛰는 규칙은 그대로다."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        die_unreadable_subs(path, f"글자가 깨져 있다: {e}")
    if raw == "":
        die_unreadable_subs(path, "파일이 비어 있다")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        if raw.startswith("\ufeff"):
            die_unreadable_subs(path, f"Unexpected UTF-8 BOM: {e}")
        die_unreadable_subs(path, str(e))
    if not isinstance(data, dict):
        die_unreadable_subs(path, f"{type(data).__name__} 이라서 events 칸을 읽을 수 없다")
    rows = []
    dropped_with_text = []
    for event in data.get("events") or []:
        if event.get("aAppend") == 1:
            text = event_text(event)
            if "".join(text.split()):
                dropped_with_text.append((int(event.get("tStartMs", 0)), text.replace("\n", " ")))
            continue
        text = event_text(event)
        if not "".join(text.split()):
            continue
        rows.append((int(event.get("tStartMs", 0)), text.replace("\n", " ")))
    return rows, dropped_with_text


def group(rows, chapters):
    groups = [[] for _ in chapters]
    for t_ms, text in rows:
        groups[chapter_of(t_ms / 1000, chapters)].append((t_ms, text))
    return groups


def main():
    out = Path(sys.argv[1])
    src = out / "원본자막"
    meta = json.loads((src / "meta.json").read_text(encoding="utf-8"))
    # 주소 없는 내 컴퓨터 영상은 자막_출처.json 이 없고 meta.json 에 id 가 없을 수 있다. 소리 받아쓰기(transcribe.py)와 같은 규칙으로 맞춘다.
    picked_file = src / "자막_출처.json"
    picked = json.loads(picked_file.read_text(encoding="utf-8")).get("고른_자막", {}) if picked_file.exists() else {}
    vid = meta.get("id")
    if not vid:
        names = sorted(
            p for p in (out / "원본영상").glob("*")
            if p.is_file() and not p.name.startswith(".")
        ) if (out / "원본영상").is_dir() else []
        vid = names[0].stem.split("_")[0] if names else "video"
    vid = str(vid)
    chapters = normalize_chapters(meta.get("chapters"), meta.get("duration") or 0)

    missing = []
    dropped_alerts = []
    tracks = []
    for lang, kind in picked.items():
        path = src / f"{vid}.{lang}.json3"
        if not path.exists():
            sys.stderr.write(f"[경고] 고른 자막 파일이 없다 = {path.name}\n")
            missing.append((lang, kind))
            continue
        try:
            rows, dropped = load_rows(path)
        except Exception as e:
            if isinstance(e, SystemExit):
                raise
            die_unreadable_subs(path, str(e))
        tracks.append({"lang": lang, "kind": kind, "rows": rows})
        for t_ms, text in dropped:
            dropped_alerts.append(f"{lang} ({kind}) {fmt(t_ms // 1000)} 버린 글자 = {text}")
    tracks.sort(key=lambda t: kind_rank(t["kind"]))
    warn_other_video_subs(src, vid, len(tracks))
    empty_warn = "⚠ 옮긴 자막이 하나도 없다 — 말한 내용은 이 파일에 없다"
    if not picked:
        # 자막 없는 영상 — 화면 글자 인식만으로 merge.py 가 돌 수 있게 빈 자막 목록을 남긴다
        (out / "자막_줄.json").write_text(
            json.dumps({"chapters": chapters, "tracks": []}, ensure_ascii=False), encoding="utf-8"
        )
        (out / "자막_전문.md").write_text(
            f"# {safe_title(meta.get('title'))}\n\n{empty_warn}\n\n이 영상에는 받을 수 있는 자막이 없다. 말한 내용은 옮기지 못했다.\n",
            encoding="utf-8",
        )
        print(empty_warn)
        return

    up = meta.get("upload_date") or ""
    lines = [
        f"# {safe_title(meta.get('title'))}",
        "",
        f"- 채널: {meta.get('channel') or meta.get('uploader') or ''}",
        f"- URL: {meta.get('webpage_url') or ''}",
        f"- 올린 날짜: {up[:4]}-{up[4:6]}-{up[6:8]}" if up else "- 올린 날짜: (없음)",
        *duration_lines(meta),
        "- 자막: " + " · ".join(f"{t['lang']} = {t['kind']}" for t in tracks),
        "- 자동 자막은 고유명사를 잘못 알아들은 것도 고치지 않고 그대로 둔다",
        "",
        "## 영상 설명란",
        "",
    ]
    lines += [escape_heading(l) for l in (meta.get("description") or "(없음)").splitlines()]
    lines += ["", "## 챕터 표", "", "시작시각 | 제목", "---|---"]
    lines += [f"{fmt(c['start_time'])} | {safe_title(c.get('title'))}" for c in chapters]
    lines.append("")

    for t in tracks:
        lines += [f"## 자막 — {safe_title(t['lang'])} ({safe_title(t['kind'])})", ""]
        for ch, rows in zip(chapters, group(t["rows"], chapters)):
            lines += [f"### {fmt(ch['start_time'])} {safe_title(ch.get('title'))}", ""]
            lines += [f"[{fmt(ms // 1000)}] {text}" for ms, text in rows]
            lines.append("")

    for t in tracks:
        lines += [f"## 이어쓰기 — {safe_title(t['lang'])} ({safe_title(t['kind'])})", ""]
        for ch, rows in zip(chapters, group(t["rows"], chapters)):
            lines += [f"### {fmt(ch['start_time'])} {safe_title(ch.get('title'))}", "", " ".join(text for _, text in rows), ""]

    lines += ["## 검산", "", "자막 | 원본 줄 수 | md 줄 수 | 첫 시각 | 마지막 시각", "---|---|---|---|---"]
    md_path = out / "자막_전문.md"
    # 검산 = 고른 자막 파일이 있는지, 버린 줄에 글자가 있었는지, 쓴 md 글자가 원본과 한 줄씩 같은지
    body = "\n".join(lines) + "\n"
    md_path.write_text(body, encoding="utf-8")
    written = md_path.read_text(encoding="utf-8").splitlines()
    problems, table = [], []
    for lang, kind in missing:
        table.append(f"{lang} ({kind}) | 파일 없음 |  |  | ")
        problems.append(f"{lang} 고른 자막 파일이 없다")
    for msg in dropped_alerts:
        problems.append(msg)
        sys.stderr.write(f"[오류] {msg}\n")
    for t in tracks:
        head = f"## 자막 — {safe_title(t['lang'])} ({safe_title(t['kind'])})"
        i = written.index(head) + 1
        md_texts = []
        while i < len(written) and not written[i].startswith("## "):
            line = written[i]
            if line.startswith("[") and "] " in line:
                md_texts.append(line.split("] ", 1)[1])
            i += 1
        src_texts = [text for _, text in t["rows"]]
        if md_texts != src_texts:
            diff = next((k for k, (a, b) in enumerate(zip(src_texts, md_texts)) if a != b), None)
            problems.append(f"{t['lang']} 원본 {len(src_texts)}줄 · md {len(md_texts)}줄 · 처음 다른 줄 = {diff}")
        rows = t["rows"]
        table.append(
            f"{t['lang']} ({t['kind']}) | {len(src_texts)} | {len(md_texts)} | "
            f"{fmt(rows[0][0] // 1000) if rows else ''} | {fmt(rows[-1][0] // 1000) if rows else ''}"
        )
    md_path.write_text(body + "\n".join(table) + "\n", encoding="utf-8")

    (out / "자막_줄.json").write_text(
        json.dumps({"chapters": chapters, "tracks": tracks}, ensure_ascii=False), encoding="utf-8"
    )
    for row in table:
        print("검산 " + row)
    if problems:
        sys.stderr.write("[오류] 검산 어긋남\n" + "\n".join(problems) + "\n")
        sys.exit(1)
    print(f"검산 통과 — 자막 {len(tracks)}개 모두 원본과 md 가 한 줄도 다르지 않다")


if __name__ == "__main__":
    main()
