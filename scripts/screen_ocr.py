# 영상을 훑어 화면·자막 영역 글자를 시각과 함께 남긴다. 자막 영역은 촘촘히, 화면 영역은 덜 촘촘히 본다
# 쓰는 법: python3 screen_ocr.py <작업 폴더> [--lang korean|en] [--band 0.70 0.97] [--band-lang korean|en]
#          [--band-step 0.25] [--screen-step 0.5] [--band-pix 40] [--band-count 120] [--screen-diff 3]
#          [--step 1.0] [--diff 25] [--from 0] [--to 초] [--jobs 1] [--limit 0] [--same-ratio 0.85]
#          [--out-name 이름]   (이름을 주면 결과는 분석/ 아래에만 쓴다)
#   truth   python3 screen_ocr.py truth <작업 폴더> --from 0 --to 40 --out-name t1 [--band 0.76 0.96]
#   compare python3 screen_ocr.py compare <작업 폴더> --truth t1 --runs v2 [--from 0 --to 40]
#   --step·--diff 는 --screen-step·--screen-diff 와 같은 뜻(옛 명령 호환). --limit 시간 재기도 같은 값을 쓴다
import argparse
import difflib
try:
    import fcntl
except ImportError:
    fcntl = None
import json
import logging
import multiprocessing as mp
import os
import re
import sys
import time

# 사용자 부품 폴더(~/.local)의 urllib3 가 다른 프로그램 때문에 옛 판으로 내려가면 paddleocr 를 못 부른다(2026-10-01).
# 스킬 전용 부품 폴더가 있으면 먼저 본다. 만드는 법은 check_env.sh 가 안내한다.
_PYFIX = os.path.expanduser("~/.cache/video-to-claude/pyfix")
if os.path.isdir(_PYFIX) and _PYFIX not in sys.path:
    sys.path.insert(0, _PYFIX)
    os.environ["PYTHONPATH"] = _PYFIX + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")
from pathlib import Path

PROGRESS_NAME = "화면글자_진행.json"
LOCK_NAME = "화면글자.lock"
SAME_LINE_DEFAULT = 0.85

from common import OCR_MARK, fmt, repeated_lines

os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
try:
    import cv2  # noqa: E402
except ImportError:
    _req = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "requirements-ocr.txt"))
    sys.stderr.write(f"cv2 없음 — python3 -m pip install --user -r \"{_req}\"\n")
    sys.exit(1)
import numpy as np  # noqa: E402


def kst():
    # 이 컴퓨터의 시간대로 찍는다(받기 스크립트 fetch.sh 와 같은 기준)
    return time.strftime("%H:%M:%S %Z")


def norm(s):
    return re.sub(r"\s+", "", s).lower()


def meaningful(s):
    return len(re.findall(r"[0-9A-Za-z가-힣]", s)) >= 2


def same_line(a, b, ratio):
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= ratio


def make_ocr(lang, det_model=None, rec_model=None):
    from paddleocr import PaddleOCR

    logging.getLogger("ppocr").setLevel(logging.ERROR)
    kw = {
        "lang": lang,
        "enable_mkldnn": False,
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
    }
    if det_model or rec_model:
        # 모델 이름을 하나라도 주면 paddleocr 가 lang 을 무시하고 나머지를 중국어·영어용 기본으로 채우므로 한쪽만 줘도 다른 쪽은 기존 기본값으로 고정한다
        kw["text_detection_model_name"] = det_model or "PP-OCRv5_server_det"
        kw["text_recognition_model_name"] = rec_model or {"korean": "korean_PP-OCRv5_mobile_rec", "en": "en_PP-OCRv5_mobile_rec"}.get(lang)
        if kw["text_recognition_model_name"] is None:
            del kw["text_recognition_model_name"]
    return PaddleOCR(**kw)


def read_lines_scored(ocr, img):
    """글자 줄과 줄마다 인식 점수를 위→아래, 왼쪽→오른쪽 순으로 돌려준다."""
    items = []
    for r in ocr.predict(img):
        d = r if isinstance(r, dict) else r.json.get("res", {})
        texts = d.get("rec_texts") or []
        scores = d.get("rec_scores") or []
        boxes = d.get("rec_polys")
        if boxes is None:
            boxes = d.get("dt_polys")
        for i, raw in enumerate(texts):
            text = (raw or "").strip()
            if not text:
                continue
            y = x = 0.0
            if boxes is not None and i < len(boxes):
                pts = np.asarray(boxes[i]).reshape(-1, 2)
                y, x = float(pts[:, 1].min()), float(pts[:, 0].min())
            sc = float(scores[i]) if i < len(scores) else None
            items.append((round(y / 12), x, text, sc))
    items.sort()
    lines = [t for _, _, t, _ in items]
    rec_scores = [s for _, _, _, s in items]
    return lines, rec_scores


def open_video(out):
    videos = [v for v in sorted((out / "원본영상").glob("*_720p.*")) if v.suffix != ".txt"]
    if not videos:
        raise SystemExit("[오류] 원본영상 폴더에 영상 파일이 없다 — fetch.sh 에 --video 를 줘서 먼저 받아라")
    if len(videos) > 1:
        print(f"[경고] 원본영상 폴더에 720p 영상이 {len(videos)}개 있다 — 첫 파일 {videos[0].name} 을 쓴다", flush=True)
    cap = cv2.VideoCapture(str(videos[0]))
    if not cap.isOpened():
        raise SystemExit("[오류] 영상 파일을 열지 못했다")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    nframes = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = nframes / fps if nframes > 0 else 0.0
    ok, _ = cap.read()
    if not ok:
        cap.release()
        raise SystemExit(
            "[오류] 영상 첫 장면을 못 읽었다 — AV1 이면 OpenCV 가 못 푼다. "
            "원본영상 폴더의 영상 파일을 지우고 fetch.sh --video 를 다시 돌려라"
        )
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    return cap, videos[0], fps, height, total


def resolve_screen_timing(args):
    """시간 재기(--limit)도 본 실행과 같은 값을 쓴다 — 설정이 다르면 잰 시간이 쓸모없다."""
    step = args.step if args.step is not None else args.screen_step
    diff = args.diff if args.diff is not None else args.screen_diff
    return float(step), float(diff)


def timing_params(args):
    band_step = float(args.band_step)
    screen_step, screen_diff = resolve_screen_timing(args)
    return {
        "band_step": band_step,
        "screen_step": screen_step,
        "band_pix": int(args.band_pix),
        "band_count": int(args.band_count),
        "screen_diff": screen_diff,
        "same_line": float(args.same_ratio),
    }


def settings_from_args(args, tp):
    return {
        "band_step": tp["band_step"],
        "screen_step": tp["screen_step"],
        "band_pix": tp["band_pix"],
        "band_count": tp["band_count"],
        "screen_diff": tp["screen_diff"],
        "band": [float(args.band[0]), float(args.band[1])] if args.band is not None else None,
        "lang": args.lang,
        "band_lang": args.band_lang,
        "same_line": tp["same_line"],
        "from": float(args.frm),
        "to": float(args.to) if args.to is not None else None,
        "jobs": int(args.jobs),
        "det_model": getattr(args, "det_model", None),
        "rec_model": getattr(args, "rec_model", None),
    }


def settings_equal(saved, now):
    if not isinstance(saved, dict) or not isinstance(now, dict):
        return False
    if saved.get("lang") != now.get("lang"):
        return False
    if saved.get("band_lang") != now.get("band_lang"):
        return False
    if saved.get("det_model") != now.get("det_model"):
        return False
    if saved.get("rec_model") != now.get("rec_model"):
        return False
    for key in ("band_step", "screen_step", "screen_diff", "same_line", "band_pix", "band_count", "from", "jobs"):
        try:
            if abs(float(saved.get(key, 0)) - float(now.get(key, 0))) > 1e-9:
                return False
        except (TypeError, ValueError):
            return False
    to_a, to_b = saved.get("to"), now.get("to")
    if (to_a is None) != (to_b is None):
        return False
    if to_a is not None and abs(float(to_a) - float(to_b)) > 1e-9:
        return False
    band_a, band_b = saved.get("band"), now.get("band")
    if band_a is None and band_b is None:
        return True
    if not isinstance(band_a, (list, tuple)) or not isinstance(band_b, (list, tuple)):
        return False
    if len(band_a) != 2 or len(band_b) != 2:
        return False
    try:
        return abs(float(band_a[0]) - float(band_b[0])) < 1e-9 and abs(float(band_a[1]) - float(band_b[1])) < 1e-9
    except (TypeError, ValueError):
        return False


def acquire_lock(out):
    fh = open(out / LOCK_NAME, "a")
    if fcntl is None:
        sys.stderr.write("[알림] fcntl 없음 — 이 환경에서는 작업 폴더 잠금을 건너뛴다\n")
        return fh
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise SystemExit("[오류] 이 작업 폴더에서 화면 글자 인식이 이미 돌고 있다")
    return fh


def front_to_json(last_front):
    out = {}
    for name, val in last_front.items():
        if val is None:
            out[name] = None
        elif isinstance(val, np.ndarray):
            out[name] = val.tolist()
        else:
            out[name] = val
    return out


def front_from_json(raw, names):
    last = {}
    front = raw or {}
    for name in names:
        v = front.get(name)
        if v is None:
            last[name] = None
        elif name == "화면":
            last[name] = np.asarray(v, dtype=np.float32)
        else:
            last[name] = np.asarray(v, dtype=np.int16)
    return last


def save_progress(path, last_t, settings, reads, last_front, n_ocr):
    payload = {
        "마지막시각": last_t,
        "설정": settings,
        "읽기": reads,
        "앞장면": front_to_json(last_front),
        "글자인식횟수": n_ocr,
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load_progress(path, settings, front_names):
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        print("[알림] 화면글자_진행.json 을 읽지 못해 처음부터 읽는다", flush=True)
        return None
    if not settings_equal(data.get("설정"), settings):
        print("[알림] 화면글자_진행.json 의 설정이 달라 처음부터 읽는다", flush=True)
        return None
    reads = data.get("읽기")
    if reads is None and data.get("항목"):
        print("[알림] 화면글자_진행.json 형식이 옛날이라 처음부터 읽는다", flush=True)
        return None
    if not isinstance(reads, list):
        print("[알림] 화면글자_진행.json 을 읽지 못해 처음부터 읽는다", flush=True)
        return None
    try:
        last_t = float(data.get("마지막시각"))
    except (TypeError, ValueError):
        print("[알림] 화면글자_진행.json 을 읽지 못해 처음부터 읽는다", flush=True)
        return None
    last_front = front_from_json(data.get("앞장면"), front_names)
    try:
        n_ocr = int(data.get("글자인식횟수") or 0)
    except (TypeError, ValueError):
        n_ocr = 0
    print(f"[알림] {fmt(last_t)} 다음부터 이어 읽는다", flush=True)
    return reads, last_t, last_front, n_ocr


def combine_reads(reads, line_ratio):
    items = []
    for region in ("화면", "자막띠"):
        rr = [r for r in reads if r.get("region") == region]
        history = []
        for r in rr:
            recent = [x for h in history for x in h]
            new_lines, new_scores, now = [], [], []
            lines = r.get("lines") or []
            scores = r.get("rec_scores") or []
            if len(scores) != len(lines):
                scores = [None] * len(lines)
            for line, sc in zip(lines, scores):
                if not meaningful(line):
                    continue
                n = norm(line)
                hit = next((it for m, it in recent if same_line(n, m, line_ratio)), None)
                if hit is not None:
                    hit["end"] = max(hit["end"], r["t"])
                    now.append((n, hit))
                else:
                    new_lines.append(line)
                    new_scores.append(sc)
                    now.append((n, None))
            if new_lines:
                item = {
                    "region": region,
                    "start": r["t"],
                    "end": r["t"],
                    "text": "\n".join(new_lines),
                    "rec_scores": new_scores,
                }
                items.append(item)
                now = [(n, it if it is not None else item) for n, it in now]
            history = (history + [now])[-3:]
    items.sort(key=lambda it: (it["start"], it["region"]))
    for it in items:
        it["start"], it["end"] = round(it["start"], 1), round(it["end"], 1)
    return dedup_substring(items)


def dedup_substring(items):
    """2초 안 이웃 항목에서 한쪽 글이 다른 쪽에 통째로 들어 있으면 긴 쪽만 남긴다."""
    if not items:
        return items
    by_region = {}
    for it in items:
        by_region.setdefault(it["region"], []).append(it)
    out = []
    for region, group in by_region.items():
        group.sort(key=lambda x: x["start"])
        drop = set()
        for i, a in enumerate(group):
            if i in drop:
                continue
            na = norm(a["text"])
            for j in range(i + 1, len(group)):
                if j in drop:
                    continue
                b = group[j]
                if b["start"] - a["start"] > 2.0:
                    break
                nb = norm(b["text"])
                if na in nb and na != nb:
                    drop.add(i)
                    break
                if nb in na and na != nb:
                    drop.add(j)
        out.extend(it for k, it in enumerate(group) if k not in drop)
    out.sort(key=lambda it: (it["start"], it["region"]))
    return out


def scan_reads(
    out_dir,
    frm,
    to_sec,
    band,
    band_lang,
    lang,
    tp,
    truth=False,
    resume=None,
    ocr_limit=0,
    progress=None,
    frm_wall=None,
    det_model=None,
    rec_model=None,
):
    cap, video, fps, height, total = open_video(out_dir)
    to_sec = min(float(to_sec), total) if total > 0 else float(to_sec)
    if frm >= to_sec:
        cap.release()
        return [], video.name, total, 0
    y0 = y1 = 0
    use_band = band is not None
    if use_band:
        y0, y1 = int(band[0] * height), int(band[1] * height)
    band_every = max(1, int(tp["band_step"] * fps)) if use_band else 0
    screen_every = max(1, int(tp["screen_step"] * fps))
    def _ocr_for(lg):
        return make_ocr(lg, det_model, rec_model)

    ocrs = {lang: _ocr_for(lang)}
    if use_band:
        bl = band_lang or lang
        if bl not in ocrs:
            ocrs[bl] = _ocr_for(bl)
    reads, done_until, n_ocr = [], -1.0, 0
    last_band = last_screen = None
    front_names = (["화면", "자막띠"] if use_band else ["화면"])
    if resume:
        reads, done_until, last_front, n_ocr = resume
        last_band = last_front.get("자막띠")
        last_screen = last_front.get("화면")
    else:
        last_front = {n: None for n in front_names}
    start_idx = int(frm * fps)
    if use_band:
        start_idx -= start_idx % band_every
    else:
        start_idx -= start_idx % screen_every
    if done_until >= 0:
        start_idx = max(start_idx, int(done_until * fps) + 1)
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, start_idx))
    idx = max(0, start_idx)
    last_save_at = time.time()
    t0 = time.time()
    if frm_wall is None:
        frm_wall = float(frm)
    while True:
        if not cap.grab():
            break
        t = idx / fps
        if t > to_sec:
            break
        do_band = use_band and idx % band_every == 0
        do_screen = (not truth) and idx % screen_every == 0
        if not use_band:
            do_screen = idx % screen_every == 0
            do_band = False
        idx += 1
        if not (do_band or do_screen) or t <= done_until:
            continue
        ok, frame = cap.retrieve()
        if not ok or frame is None or frame.size == 0:
            continue
        if do_band:
            crop = frame[y0:y1]
            if crop.shape[0] == 0:
                continue
            g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).astype(np.int16)
            changed = (
                last_band is None
                or truth
                or int((np.abs(g - last_band) > tp["band_pix"]).sum()) >= tp["band_count"]
            )
            if changed:
                last_band = g
                last_front["자막띠"] = g
                lines, scores = read_lines_scored(ocrs[band_lang or lang], crop)
                reads.append({"t": round(t, 3), "region": "자막띠", "lines": lines, "rec_scores": scores})
                n_ocr += 1
                if ocr_limit and n_ocr >= ocr_limit:
                    break
        if ocr_limit and n_ocr >= ocr_limit:
            break
        if do_screen:
            if use_band:
                pieces = [p for p in (frame[:y0], frame[y1:]) if p.shape[0] >= 10]
                crop = np.vstack(pieces) if pieces else None
            else:
                crop = frame
            if crop is None or crop.shape[0] == 0:
                continue
            small = cv2.resize(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), (64, 36)).astype(np.float32)
            if last_screen is None or float(np.abs(small - last_screen).mean()) >= tp["screen_diff"]:
                last_screen = small
                last_front["화면"] = small
                lines, scores = [], []
                if use_band:
                    for p in (frame[:y0], frame[y1:]):
                        if p.shape[0] >= 10:
                            ln, sc = read_lines_scored(ocrs[lang], p)
                            lines += ln
                            scores += sc
                else:
                    lines, scores = read_lines_scored(ocrs[lang], crop)
                reads.append({"t": round(t, 3), "region": "화면", "lines": lines, "rec_scores": scores})
                n_ocr += 1
                if ocr_limit and n_ocr >= ocr_limit:
                    break
        done_until = t
        if n_ocr > 0 and n_ocr % 20 == 0:
            spent = time.time() - t0
            done = t - frm_wall
            left = spent / max(done, 0.1) * (to_sec - t) if not ocr_limit else 0
            msg = (
                f"[{kst()}] {fmt(t)}/{fmt(to_sec)} · 글자 인식 {n_ocr}번 · "
                f"걸린 {spent / 60:.1f}분"
            )
            if not ocr_limit:
                msg += f" · 남은 약 {left / 60:.0f}분"
            print(msg, flush=True)
        if progress and n_ocr > 0 and (time.time() - last_save_at >= 30 or n_ocr % 5 == 0):
            save_progress(progress["path"], t, progress["settings"], reads, last_front, n_ocr)
            last_save_at = time.time()
        if ocr_limit and n_ocr >= ocr_limit:
            break
    cap.release()
    return reads, video.name, total, n_ocr, t if reads else frm


def worker_scan(kwargs):
    reads, _, _, n_ocr, _ = scan_reads(**kwargs)
    return reads, n_ocr


def output_paths(out, args, limit_mode):
    if args.out_name:
        base = out / "분석" / args.out_name
        base.parent.mkdir(parents=True, exist_ok=True)
        return base.with_name(f"{args.out_name}_원본.json"), base.with_name(f"{args.out_name}.md"), f"구간_{args.out_name}"
    if limit_mode:
        return out / "화면글자_시험_원본.json", out / "화면글자_시험.md", None
    # 본 실행도 읽은 것 전부를 분석/구간_본.json 에 남긴다. 놓침 검산(compare --runs 본)이 이 파일을 연다.
    (out / "분석").mkdir(parents=True, exist_ok=True)
    return out / "화면글자_OCR_원본.json", out / "화면글자_OCR.md", "구간_본"


def guard_overwrite_raw(out, args, limit_mode, settings):
    if args.out_name or limit_mode:
        return
    raw_path = out / "화면글자_OCR_원본.json"
    if not raw_path.is_file():
        return
    구간_path = out / "분석" / "구간_본.json"
    if not 구간_path.is_file():
        return
    try:
        prev = json.loads(구간_path.read_text(encoding="utf-8")).get("settings")
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return
    if prev is None or settings_equal(prev, settings):
        return
    raise SystemExit(
        "[오류] 화면글자_OCR_원본.json 이 이미 있고 이번 구간·설정이 이전과 다르다. "
        "덮어쓰지 않았다. 다른 구간은 --out-name 으로 분석/ 아래에만 저장한 뒤 합치기는 손으로 한다"
    )


def run_ocr(args):
    out = Path(args.out)
    if args.band is not None:
        b0, b1 = args.band
        if not (0.0 <= b0 <= 1.0 and 0.0 <= b1 <= 1.0 and b0 < b1):
            raise SystemExit("[오류] --band 는 0~1 사이의 두 숫자이고 앞이 뒤보다 작아야 한다")
    cap, video_name, _, _, total = open_video(out)
    cap.release()
    if args.to is None:
        args.to = total
    if float(args.frm) >= float(total):
        raise SystemExit(f"[오류] --from {args.frm} 이 영상 길이 {fmt(total)} 밖이다")
    limit_mode = bool(args.limit)
    if limit_mode and args.jobs > 1:
        raise SystemExit("[오류] --limit 과 --jobs 2 이상은 같이 쓸 수 없다. 시간 재기는 --jobs 1 로 돌린다")
    tp = timing_params(args)
    settings = settings_from_args(args, tp)
    guard_overwrite_raw(out, args, limit_mode, settings)
    lock_fh = acquire_lock(out) if not args.out_name else None
    try:
        t0 = time.time()
        progress_path = out / PROGRESS_NAME
        resume = None
        if not limit_mode and args.jobs == 1 and not args.out_name:
            loaded = load_progress(progress_path, settings, ["화면", "자막띠"] if args.band else ["화면"])
            if loaded:
                resume = loaded
        progress = None
        if not limit_mode and args.jobs == 1 and not args.out_name:
            progress = {"path": progress_path, "settings": settings}
        print(
            f"[{kst()}] 시작 · 영상 {fmt(total)} · 읽기 {fmt(args.frm)}~{fmt(args.to)} · "
            f"영역 {['화면', '자막띠'] if args.band else ['화면']}",
            flush=True,
        )
        all_reads = []
        n_ocr = 0
        idx_end = float(args.frm)
        if args.jobs > 1:
            chunks = []
            span = float(args.to) - float(args.frm)
            step = span / args.jobs
            for i in range(args.jobs):
                a = float(args.frm) + step * i
                b = float(args.to) if i == args.jobs - 1 else float(args.frm) + step * (i + 1)
                chunks.append((a, b))
            ctx = mp.get_context("spawn")
            tasks = [
                {
                    "out_dir": out,
                    "frm": a,
                    "to_sec": b,
                    "band": settings.get("band"),
                    "band_lang": args.band_lang,
                    "lang": args.lang,
                    "tp": tp,
                    "truth": False,
                    "resume": None,
                    "ocr_limit": 0,
                    "progress": None,
                    "frm_wall": float(args.frm),
                    "det_model": args.det_model,
                    "rec_model": args.rec_model,
                }
                for a, b in chunks
            ]
            with ctx.Pool(args.jobs) as pool:
                results = pool.map(worker_scan, tasks)
            for reads, n in results:
                all_reads.extend(reads)
                n_ocr += n
            idx_end = float(args.to)
        else:
            reads, _, _, n_ocr, idx_end = scan_reads(
                out,
                float(args.frm),
                float(args.to),
                settings.get("band"),
                args.band_lang,
                args.lang,
                tp,
                truth=False,
                resume=resume,
                ocr_limit=args.limit if limit_mode else 0,
                progress=progress,
                frm_wall=float(args.frm),
                det_model=args.det_model,
                rec_model=args.rec_model,
            )
            all_reads = reads
        all_reads.sort(key=lambda r: (r["t"], r["region"]))
        items = combine_reads(all_reads, tp["same_line"])
        spent = time.time() - t0
        raw_path, md_path, reads_stem = output_paths(out, args, limit_mode)
        raw_path.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        if reads_stem:
            target_to = float(args.to)
            finished = idx_end >= target_to - 1e-6
            (out / "분석" / f"{reads_stem}.json").write_text(
                json.dumps(
                    {
                        "settings": settings,
                        "done_until": float(idx_end),
                        "finished": finished,
                        "reads": all_reads,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        write_md(
            md_path, video_name, args, tp, args.frm, args.to, total, items, n_ocr, spent, idx_end
        )
        if not limit_mode and not args.out_name and progress_path.is_file():
            progress_path.unlink()
        if limit_mode:
            left = spent / max(idx_end - float(args.frm), 0.1) * (total - idx_end)
            print(f"[{kst()}] 시간 재기 끝 · 영상 전체를 읽으면 남은 약 {left / 60:.0f}분", flush=True)
        n_text = sum(1 for it in items if it["text"].strip())
        last_line = (
            f"[{kst()}] 끝 · 훑은 끝 {fmt(idx_end)}/{fmt(total)} · 글자 인식 {n_ocr}번 · "
            f"글자 있는 항목 {n_text} · {spent / 60:.1f}분"
        )
        print(last_line, flush=True)
        return last_line
    finally:
        if lock_fh:
            if fcntl is not None:
                try:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            lock_fh.close()


def write_md(md_path, video_name, args, tp, frm, to_sec, total, items, n_ocr, spent, idx_end):
    repeated = repeated_lines(items)
    band_note = ""
    if args.band:
        band_note = f", 자막띠={args.band_lang or args.lang}, 자막띠 세로 범위 {args.band[0]}~{args.band[1]}"
    lines = [
        f"# 화면 글자 인식 결과 — {video_name}",
        "",
        f"- 읽은 범위 {fmt(frm)} ~ {fmt(min(to_sec, total))} (영상 길이 {fmt(total)})",
        f"- 자막 영역 {tp['band_step']}초마다(픽셀 {tp['band_pix']}·{tp['band_count']}개), "
        f"화면 영역 {tp['screen_step']}초마다(축소 평균 차 {tp['screen_diff']})",
        f"- PaddleOCR, 화면={args.lang}{band_note}",
        f"- 같은 줄은 {tp['same_line']:.0%} 이상 닮으면 끝 시각만 늘렸다. 2초 안 포함 관계는 긴 쪽만 남겼다.",
        "- 글자 인식은 틀릴 수 있다. 중요한 글자는 장면확인 폴더 그림이나 영상으로 다시 본다.",
        "",
        "## 거의 모든 화면에 되풀이된 글자",
        "",
    ]
    lines += [f"- {l}" for l in sorted(repeated)] or ["(없음)"]
    lines += ["", "## 시각별", ""]
    n_text = 0
    for it in items:
        if not it["text"].strip():
            continue
        n_text += 1
        lines += [f"{OCR_MARK}[{fmt(it['start'])}~{fmt(it['end'])}] ({it['region']})", it["text"], ""]
    lines += [
        "## 검산",
        "",
        "훑은 끝 시각 | 글자 인식 횟수 | 글자 있는 항목 | 걸린 시간",
        "---|---|---|---",
        f"{fmt(idx_end)} / {fmt(total)} | {n_ocr} | {n_text} | {spent / 60:.1f}분",
        "",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")


def run_truth(args):
    out = Path(args.out)
    if not args.out_name:
        raise SystemExit("[오류] truth 는 --out-name 이 필요하다")
    if args.to is None:
        _, _, _, _, total = open_video(out)
        args.to = total
    tp = timing_params(args)
    reads, _, total, n_ocr, _ = scan_reads(
        out,
        float(args.frm),
        float(args.to),
        [float(args.band[0]), float(args.band[1])] if args.band else None,
        args.band_lang,
        args.lang,
        tp,
        truth=True,
        ocr_limit=0,
        progress=None,
        frm_wall=float(args.frm),
        det_model=args.det_model,
        rec_model=args.rec_model,
    )
    dest = out / "분석" / f"정답_{args.out_name}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    settings = settings_from_args(args, tp)
    dest.write_text(
        json.dumps(
            {"settings": settings, "done_until": float(args.to), "finished": True, "reads": reads},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"[{kst()}] 정답 {args.frm}~{args.to}초 · 글자 인식 {n_ocr}번 · {dest.name}", flush=True)


def load_reads_file(out, prefix, names, frm=None, to_sec=None):
    reads = []
    for name in names:
        path = out / "분석" / f"{prefix}_{name}.json"
        if not path.is_file():
            raise SystemExit(f"[오류] {path} 이 없다")
        d = json.loads(path.read_text(encoding="utf-8"))
        if not d.get("finished"):
            print(f"[경고] {path.name} 이 아직 끝나지 않았다", flush=True)
        reads += d["reads"]
    if frm is not None or to_sec is not None:
        reads = [
            r
            for r in reads
            if (frm is None or r["t"] >= frm) and (to_sec is None or r["t"] <= to_sec)
        ]
    reads.sort(key=lambda r: (r["t"], r["region"]))
    return reads


def run_compare(args):
    out = Path(args.out)
    if not args.truth or not args.runs:
        raise SystemExit("[오류] compare 는 --truth 와 --runs 가 필요하다")
    truth = load_reads_file(out, "정답", args.truth.split(","), args.frm, args.to)
    runs = [
        r
        for r in load_reads_file(out, "구간", args.runs.split(","), args.frm, args.to)
        if r["region"] == "자막띠"
    ]
    run_lines = [(r["t"], norm(l)) for r in runs for l in r["lines"] if meaningful(l)]
    missed, total, seen = [], 0, []
    ratio = float(args.same_ratio)
    for r in truth:
        for l in r["lines"]:
            if not meaningful(l):
                continue
            n = norm(l)
            if any(same_line(n, s, ratio) for s in seen):
                continue
            seen.append(n)
            total += 1
            if not any(abs(t - r["t"]) <= 3 and same_line(n, m, ratio) for t, m in run_lines):
                missed.append((r["t"], l))
    print(f"정답의 서로 다른 줄 {total}개 · run 에 없는 줄 {len(missed)}개")
    for t, l in missed:
        print(f"  놓침 {t:.2f}초 | {l}")
    return len(missed)


def add_shared_args(ap):
    ap.add_argument("out")
    ap.add_argument("--lang", default="korean")
    ap.add_argument("--band", nargs=2, type=float)
    ap.add_argument("--band-lang")
    ap.add_argument("--band-step", type=float, default=0.25)
    ap.add_argument("--screen-step", type=float, default=0.5)
    ap.add_argument("--band-pix", type=float, default=40.0)
    ap.add_argument("--band-count", type=int, default=120)
    ap.add_argument("--screen-diff", type=float, default=3.0)
    ap.add_argument("--step", type=float, default=None, help="옛 이름 — 화면 영역 간격")
    ap.add_argument("--diff", type=float, default=None, help="옛 이름 — 화면 영역 문턱")
    ap.add_argument("--from", dest="frm", type=float, default=0.0)
    ap.add_argument("--to", type=float, default=None)
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--same-ratio", type=float, default=SAME_LINE_DEFAULT)
    ap.add_argument("--out-name")
    ap.add_argument("--det-model", dest="det_model")
    ap.add_argument("--rec-model", dest="rec_model")


def main():
    argv = sys.argv[1:]
    mode = None
    if argv and argv[0] in ("truth", "compare"):
        mode = argv.pop(0)
    ap = argparse.ArgumentParser(description="화면·자막 영역 글자 인식")
    if mode == "compare":
        ap.add_argument("out")
        ap.add_argument("--truth", required=True)
        ap.add_argument("--runs", required=True)
        ap.add_argument("--from", dest="frm", type=float, default=0.0)
        ap.add_argument("--to", type=float, default=None)
        ap.add_argument("--same-ratio", type=float, default=SAME_LINE_DEFAULT)
        args = ap.parse_args(argv)
        missed = run_compare(args)
        if missed:
            raise SystemExit(missed)
        return
    add_shared_args(ap)
    if mode == "truth":
        args = ap.parse_args(argv)
        run_truth(args)
        return
    args = ap.parse_args(argv)
    if args.jobs < 1:
        raise SystemExit("[오류] --jobs 는 1 이상이어야 한다")
    run_ocr(args)


if __name__ == "__main__":
    main()
