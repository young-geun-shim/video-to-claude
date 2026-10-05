#!/usr/bin/env python3
# 자막이 없는 영상의 소리를 faster-whisper 로 받아써서 파이프라인이 읽는 자막 파일(json3)로 남긴다
# 쓰는 법: python3 transcribe.py <작업 폴더> [--model small] [--lang ko|en|...] [--from 0] [--to 초]
#   작업 폴더/원본영상/ 의 영상에서 소리를 뽑아 CPU 로 글로 바꾼다(int8, 말 없는 구간은 건너뜀).
#   결과 = 원본자막/<영상번호>.<언어>-whisper.json3 + 원본자막/자막_출처.json 에 이름표 한 줄 추가.
#   이름표 = 「발화 원문 · 소리 받아쓰기(whisper)」. 이어서 subs_to_md.py → merge.py 를 돌린다.
#   fetch.sh 를 다시 돌리면 자막_출처.json 이 새로 써지므로, fetch.sh 뒤에 이 스크립트를 돌린다.
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

KIND_WHISPER = "발화 원문 · 소리 받아쓰기(whisper)"
VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".mp3", ".m4a", ".wav", ".opus", ".ogg"}


def kst():
    return time.strftime("%H:%M:%S KST", time.gmtime(time.time() + 9 * 3600))


def log(msg):
    print(f"[transcribe {kst()}] {msg}", flush=True)


def die(msg):
    sys.stderr.write(f"[오류] {msg}\n")
    sys.exit(1)


def has_audio(path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        die("ffmpeg 가 없다 — 먼저 설치해라")
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    return "Audio:" in r.stderr


def download_audio(work, folder, vid):
    """fetch.sh --video 가 받는 720p 파일에는 소리가 없다. 영상 정보의 주소로 소리만 따로 받는다."""
    meta = work / "원본자막" / "meta.json"
    try:
        url = json.loads(meta.read_text(encoding="utf-8")).get("webpage_url")
    except (OSError, ValueError):
        url = None
    if not url:
        die("영상 파일에 소리가 없고 meta.json 에 영상 주소도 없다 — 소리가 든 파일을 원본영상/ 에 넣어라")
    yt = Path.home() / ".cache" / "video-to-claude" / "yt-dlp"
    ytdlp = str(yt) if yt.exists() else shutil.which("yt-dlp")
    if not ytdlp:
        die("yt-dlp 가 없다 — fetch.sh 를 한 번 돌려 받아라")
    cmd = [ytdlp, "-f", "bestaudio", "--no-part", "-o", str(folder / f"{vid}_audio.%(ext)s")]
    node = shutil.which("node")
    if node:
        cmd += ["--js-runtimes", f"node:{node}"]
    log("영상 파일에 소리가 없어 소리만 따로 받는다")
    r = subprocess.run(cmd + [url], capture_output=True, text=True)
    if r.returncode != 0:
        die(f"소리 받기 실패 = {r.stderr.strip()[-300:]}")
    found = sorted(folder.glob(f"{vid}_audio.*"))
    if not found:
        die("소리를 받았다는데 파일이 없다")
    return found[0]


def find_media(work, vid):
    folder = work / "원본영상"
    if not folder.is_dir():
        die(f"{folder} 폴더가 없다. 영상 파일을 먼저 받아라(fetch.sh <주소> <작업 폴더> --video)")
    files = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXT and not p.name.startswith(".")
    )
    for p in files:
        if has_audio(p):
            return p
    if files and not any(has_audio(p) for p in files):
        return download_audio(work, folder, vid)
    die(f"{folder} 에 영상·소리 파일이 없다")


def video_id(work, media=None):
    meta = work / "원본자막" / "meta.json"
    if meta.exists():
        try:
            vid = json.loads(meta.read_text(encoding="utf-8")).get("id")
            if vid:
                return str(vid)
        except (OSError, ValueError):
            pass
    names = sorted(p for p in (work / "원본영상").glob("*") if p.suffix.lower() in VIDEO_EXT)
    return names[0].stem.split("_")[0] if names else "video"


def extract_audio(media, wav, start, end):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        die("ffmpeg 가 없다 — 먼저 설치해라")
    cmd = [ffmpeg, "-y", "-loglevel", "error"]
    if start:
        cmd += ["-ss", str(start)]
    if end:
        cmd += ["-t", str(end - start)]
    cmd += ["-i", str(media), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)]
    log("소리 뽑는 중")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        die(f"ffmpeg 소리 뽑기 실패 = {r.stderr.strip()[-300:]}")


def read_wav(wav):
    """16kHz 모노 wav 를 float 배열로 읽는다. faster-whisper 가 av 로 직접 열면 av 판에 따라 깨져(av 19 실측) 배열로 준다."""
    import numpy as np

    with wave.open(str(wav), "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("work", help="작업 폴더")
    ap.add_argument("--model", default="small", help="tiny·base·small·medium·large-v3 (기본 small)")
    ap.add_argument("--lang", default=None, help="말하는 언어 코드(기본 자동 판별)")
    ap.add_argument("--from", dest="start", type=float, default=0.0, help="시작 초")
    ap.add_argument("--to", dest="end", type=float, default=0.0, help="끝 초(0 = 끝까지)")
    ap.add_argument("--threads", type=int, default=0, help="CPU 스레드 수(0 = 기본)")
    args = ap.parse_args()

    work = Path(args.work)
    if args.end and args.end <= args.start:
        die("--to 는 --from 보다 커야 한다")
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        die("faster-whisper 가 없다 — python3 -m pip install --user faster-whisper")

    vid = video_id(work)
    media = find_media(work, vid)
    log(f"영상 = {media.name} · 번호 = {vid} · 모델 = {args.model} · CPU {os.cpu_count()}개")
    src = work / "원본자막"
    src.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="v2c_whisper_") as tmp:
        wav = Path(tmp) / "audio.wav"
        extract_audio(media, wav, args.start, args.end)
        log(f"소리 뽑기 끝 ({time.time() - t0:.1f}초)")
        log("모델 불러오는 중(처음이면 모델을 내려받는다)")
        t1 = time.time()
        model = WhisperModel(args.model, device="cpu", compute_type="int8", cpu_threads=args.threads)
        log(f"모델 준비 ({time.time() - t1:.1f}초)")
        t2 = time.time()
        segments, info = model.transcribe(read_wav(wav), language=args.lang, vad_filter=True)
        lang = args.lang or info.language
        log(f"언어 = {lang} (확률 {info.language_probability:.2f}) · 받아쓰기 시작")
        offset_ms = int(args.start * 1000)
        events = []
        for seg in segments:
            text = seg.text.strip()
            if not text:
                continue
            events.append(
                {
                    "tStartMs": offset_ms + int(seg.start * 1000),
                    "dDurationMs": int((seg.end - seg.start) * 1000),
                    "segs": [{"utf8": text}],
                }
            )
            log(f"{len(events)}줄 · 영상 {offset_ms // 1000 + int(seg.end)}초까지")
        log(f"받아쓰기 끝 = {len(events)}줄 ({time.time() - t2:.1f}초)")

    if not events:
        die("받아쓴 글이 한 줄도 없다 — 소리가 없거나 말이 없는 구간이다")

    key = f"{lang}-whisper"
    out_path = src / f"{vid}.{key}.json3"
    out_path.write_text(json.dumps({"wireMagic": "pb3", "events": events}, ensure_ascii=False), encoding="utf-8")
    log(f"자막 파일 = {out_path.name}")

    # 이름표를 자막_출처.json 의 고른_자막 에 더한다(있으면 보존, 없으면 새로 만든다)
    pick_path = src / "자막_출처.json"
    pick = {}
    if pick_path.exists():
        try:
            pick = json.loads(pick_path.read_text(encoding="utf-8"))
        except ValueError:
            die(f"{pick_path.name} 를 읽을 수 없다 — 지우고 fetch.sh 를 다시 돌려라")
    pick.setdefault("영상_언어", lang)
    pick.setdefault("원래_음성_언어", [lang])
    pick.setdefault("고른_자막", {})[key] = KIND_WHISPER
    pick_path.write_text(json.dumps(pick, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"이름표 = {key} → {KIND_WHISPER}")
    log(f"끝 (전체 {time.time() - t0:.1f}초)")


if __name__ == "__main__":
    main()
