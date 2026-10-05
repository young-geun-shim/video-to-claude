# 영상에서 몇 장면을 뽑아 그림 파일로 남긴다 — 화면에 박힌 자막·슬라이드 글자가 있는지 눈으로 먼저 보기 위해
# 쓰는 법: python3 sample_frames.py <작업 폴더> [장수=6] [--even] [--flat-std 2]
#   기본 = 1초마다 화면을 견줘 **크게 바뀌는 자리**를 골라 뽑는다(고르게 나눠 뽑으면 같은 화면만 여러 장 나온다).
#   --even = 예전처럼 영상을 고르게 나눠 뽑는다.
#   --flat-std = 회색조 표준편차가 이 값보다 작으면 거의 한 색으로 보고 버리고, 같은 구간의 다음 후보를 고른다. 기본 2.
# 결과 = 장면확인/frame_순번_분-초.png
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import fmt  # noqa: E402


def pick_even(total, n):
    """영상을 고르게 나눈 시각들 — 옛 방식."""
    return [total * (0.04 + 0.92 * k / max(1, n - 1)) for k in range(n)]


def gray_std(frame):
    """고른 장면이 거의 한 색인지 볼 회색조 표준편차."""
    return float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).std())


def read_at(cap, t):
    cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
    ok, frame = cap.read()
    if not ok:
        return None
    return frame


def pick_by_change(cap, fps, total, n):
    """1초마다 화면을 견줘 구간마다 바뀐 정도가 큰 시각 후보를 큰 순서대로 돌려준다.
    고르게 나눠 뽑으면 긴 영상에서 같은 화면만 여러 장 나온다(2026-09-13 실측 = 10분 영상에서
    여섯 장이 글자 있는 화면 89개 가운데 6종류밖에 못 봤다).
    반환 = (구간 목록, 후보가 없어서 고르게 나눈 시각 또는 None).
    구간 목록의 각 원소는 (시작초, 끝초, [(바뀐정도, 시각), ...]) 이고 바뀐정도가 큰 것이 앞이다."""
    every = max(1, round(fps))
    idx, prev, scored = 0, None, []
    while True:
        if not cap.grab():
            break
        if idx % every:
            idx += 1
            continue
        ok, frame = cap.retrieve()
        if ok:
            small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (64, 36)).astype(np.float32)
            if prev is not None:
                scored.append((float(np.abs(small - prev).mean()), idx / fps))
            prev = small
        idx += 1
    groups = []
    for k in range(n):
        lo, hi = total * k / n, total * (k + 1) / n
        # 바뀐정도가 큰 순. 같으면 늦은 시각이 뒤로 가게 시각은 오름차순 보조키를 쓰지 않고 원래 점수를 유지한다.
        안에든것 = sorted(((d, t) for d, t in scored if lo <= t < hi), reverse=True)
        groups.append((lo, hi, 안에든것))
    if not scored:
        return groups, pick_even(total, n)
    return groups, None


def parse_args(argv):
    """위치 인자는 예전과 같다. --even 과 --flat-std 만 더 받는다."""
    even = False
    flat_std = 2.0
    pos = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--even":
            even = True
        elif a == "--flat-std":
            i += 1
            if i >= len(argv):
                sys.exit("[오류] --flat-std 다음에 숫자가 없다")
            flat_std = float(argv[i])
        elif a.startswith("--flat-std="):
            flat_std = float(a.split("=", 1)[1])
        elif a.startswith("--"):
            sys.exit(f"[오류] 모르는 인자 {a}")
        else:
            pos.append(a)
        i += 1
    if not pos:
        sys.exit("[오류] 작업 폴더가 없다")
    return pos, even, flat_std


def take_nonflat(cap, times, flat_std):
    """times 를 앞에서부터 보다가, 회색조 표준편차가 flat_std 이상인 첫 장면을 돌려준다.
    한 색이면 알리고 같은 구간의 다음 후보로 넘어간다. 없으면 (None, None)."""
    for t in times:
        frame = read_at(cap, t)
        if frame is None:
            continue
        std = gray_std(frame)
        if std < flat_std:
            print(
                f"[알림] {fmt(t)} 장면은 거의 한 색이라 버린다 "
                f"(표준편차 {std:.2f} < {flat_std:g}) — 같은 구간의 다음 후보를 고른다"
            )
            continue
        return t, frame
    return None, None


def main():
    args, even, flat_std = parse_args(sys.argv[1:])
    out = Path(args[0])
    n = int(args[1]) if len(args) > 1 else 6
    videos = [v for v in sorted((out / "원본영상").glob("*_720p.*")) if v.suffix != ".txt"]
    if not videos:
        sys.exit("[오류] 원본영상 폴더에 영상 파일이 없다 — fetch.sh 에 --video 를 줘서 먼저 받아라")
    if len(videos) > 1:
        names = ", ".join(v.name for v in videos)
        sys.exit(
            f"[오류] 원본영상 폴더에 720p 영상이 {len(videos)}개 있다 — {names}. "
            "하나만 남기고 다시 돌려라"
        )
    cap = cv2.VideoCapture(str(videos[0]))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = cap.get(cv2.CAP_PROP_FRAME_COUNT) / fps
    shot_dir = out / "장면확인"
    shot_dir.mkdir(exist_ok=True)
    print(f"영상 = {videos[0].name} · 길이 {total:.0f}초 · 크기 {int(cap.get(3))}x{int(cap.get(4))}")
    ok, _ = cap.read()
    if not ok:
        sys.exit(
            "[오류] 영상 첫 장면을 못 읽었다 — 압축 방식이 AV1 이면 OpenCV 가 못 푼다. "
            "원본영상 폴더의 영상 파일을 지우고 fetch.sh --video 를 다시 돌려라(H.264 를 먼저 고른다)"
        )
    if even:
        groups = []
        even_times = pick_even(total, n)
        for k in range(n):
            lo, hi = total * k / n, total * (k + 1) / n
            # 고르게 나눈 시각이 한 색이면 1초씩 뒤로 같은 구간 안을 더 본다.
            start = even_times[k]
            extra = []
            t = start
            while t < hi:
                extra.append(t)
                t += 1.0
            groups.append((lo, hi, [(0.0, x) for x in extra]))
        fallback = None
    else:
        print("화면이 바뀌는 자리를 찾는 중 — 1~2분 걸린다(--even 을 주면 고르게 나눠 뽑는다)", flush=True)
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        groups, fallback = pick_by_change(cap, fps, total, n)
        cap.release()
        cap = cv2.VideoCapture(str(videos[0]))
        if fallback is not None:
            groups = []
            for k, start in enumerate(fallback):
                lo, hi = total * k / n, total * (k + 1) / n
                extra = []
                t = start
                while t < hi:
                    extra.append(t)
                    t += 1.0
                groups.append((lo, hi, [(0.0, x) for x in extra]))

    saved = []
    for k, (lo, hi, cands) in enumerate(groups):
        times = [t for _d, t in cands]
        t, frame = take_nonflat(cap, times, flat_std)
        if frame is None:
            print(f"[경고] {fmt(lo)}~{fmt(hi)} 구간은 한 색이 아닌 장면이 없어 뽑지 않았다")
            continue
        path = shot_dir / f"frame_{k + 1}_{int(t) // 60:02d}-{int(t) % 60:02d}.png"
        cv2.imwrite(str(path), frame)
        saved.append(path)
        print(path)
    fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
    cap.release()
    if not saved:
        codec = "".join(chr((fourcc >> 8 * i) & 0xFF) for i in range(4))
        sys.exit(f"[오류] 첫 장면은 읽혔는데 중간 장면을 한 장도 못 뽑았다(압축 방식 = {codec!r}). 원본영상/받기기록.txt 에서 고른 형식을 확인하라")
    # 예전에 뽑아 둔 그림은 **새 그림을 다 저장한 뒤에** 지운다.
    # 먼저 지우고 뽑다가 실패하면 예전 것도 새것도 없어진다(2026-09-13 확인).
    keep = {p.name for p in saved}
    gone = [old for old in shot_dir.glob("frame_*.png") if old.name not in keep]
    for old in gone:
        old.unlink()
    if gone:
        print(f"예전 장면 그림 {len(gone)}장을 지웠다")


if __name__ == "__main__":
    main()
