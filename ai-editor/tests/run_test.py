#!/usr/bin/env python3
"""가짜 말소리(톤)와 가짜 받아쓰기로 autoedit.py 의 무음 컷, 재촬영 삭제, 자막, 효과음을 검사한다.
whisper 모델이 필요 없다.   python tests/run_test.py"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AE = os.path.join(ROOT, "autoedit.py")
tmp = tempfile.mkdtemp()


def run(*cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        print(r.stdout, r.stderr)
        sys.exit(1)
    return r.stdout


# 말하는 구간(초): 앞의 짧은 시도 → 멈춤 → 다시 말하기, 긴 침묵, 문장 2, 문장 3, 문장 안 재시작
bursts = [(0.5, 2.0), (3.2, 6.2), (8.5, 11.0), (11.4, 14.0), (16.0, 16.8), (17.9, 20.5)]
expr = "+".join(f"between(t,{a},{b})" for a, b in bursts)
run("ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30",
    "-f", "lavfi", "-i", f"aevalsrc='0.5*sin(2*PI*300*t)*({expr})+0.001*random(0)':d=22:s=44100",
    "-t", "22", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", f"{tmp}/raw.mp4")


def words(text, a, b):
    ws = text.split()
    n = sum(len(w) for w in ws)
    t, out = a, []
    for w in ws:
        d = (b - a) * len(w) / n
        out.append({"word": w, "start": t, "end": t + d})
        t += d
    return out


segs = [
    {"start": 0.5, "end": 2.0, "text": "오늘은 주식에 대해서 이야기를 하", "words": words("오늘은 주식에 대해서 이야기를 하", 0.5, 2.0)},
    {"start": 3.2, "end": 6.2, "text": "오늘은 주식에 대해서 이야기를 해보겠습니다.", "words": words("오늘은 주식에 대해서 이야기를 해보겠습니다.", 3.2, 6.2)},
    {"start": 8.5, "end": 11.0, "text": "먼저 기본 용어부터 알아볼게요.", "words": words("먼저 기본 용어부터 알아볼게요.", 8.5, 11.0)},
    {"start": 11.4, "end": 14.0, "text": "차트가 오르면 매수 신호라고 부르는데 항상 맞는 건 아닙니다.", "words": words("차트가 오르면 매수 신호라고 부르는데 항상 맞는 건 아닙니다.", 11.4, 14.0)},
    # 한 문장(세그먼트) 안에서 멈춤 뒤 다시 시작
    {"start": 16.0, "end": 20.5, "text": "오늘은 이제 오늘은 이제 시작합니다",
     "words": words("오늘은 이제", 16.0, 16.8) + words("오늘은 이제 시작합니다", 17.9, 20.5)},
]
json.dump({"segments": segs}, open(f"{tmp}/t.json", "w", encoding="utf-8"), ensure_ascii=False)

print(run(sys.executable, AE, "edit", f"{tmp}/raw.mp4", "--transcript", f"{tmp}/t.json", "--out", f"{tmp}/out"))
rep = json.load(open(f"{tmp}/out/report.json", encoding="utf-8"))
srt = open(f"{tmp}/out/edited.srt", encoding="utf-8").read()
print(srt)

fails = []


def check(name, ok):
    print(("통과 " if ok else "실패 ") + name)
    if not ok:
        fails.append(name)


dropped = [r["dropped"] for r in rep["retakes"]]
check("끊긴 첫 시도를 삭제", "오늘은 주식에 대해서 이야기를 하" in dropped)
check("문장 안 재시작 앞부분 삭제", "오늘은 이제" in dropped)
check("완성된 말은 남김", not any("해보겠습니다" in d or "시작합니다" in d for d in dropped))
check("길이가 줄어듦(22초 → 약 13초 이하)", rep["output_duration"] < 14)
check("자막 파일 생성", len(rep["subtitle_warnings"]) == 0 and rep["subtitle_count"] >= 3)
check("끝 마침표 제거", "해보겠습니다." not in srt and "알아볼게요." not in srt)
check("줄당 16자 이하", all(sum(1 if '가' <= c <= '힣' else 0.5 for c in ln) <= 16 for ln in srt.splitlines() if ln and "-->" not in ln and not ln.isdigit()))
check("재촬영 표현이 자막에 없음", "하\n" not in srt and "오늘은 이제 오늘은" not in srt)

# 실제 렌더 + 길이 확인
run(sys.executable, AE, "edit", f"{tmp}/raw.mp4", "--transcript", f"{tmp}/t.json", "--out", f"{tmp}/out")
d = float(run("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", f"{tmp}/out/edited.mp4"))
check(f"렌더된 길이가 계획과 일치 ({d:.1f}s)", abs(d - rep["output_duration"]) < 0.3)

# 효과음 믹스
run("ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=880:duration=0.3", f"{tmp}/pop.wav")
run(sys.executable, AE, "sfx", f"{tmp}/out/edited.mp4", "--report", f"{tmp}/out/report.json", "--cut", f"{tmp}/pop.wav", "--intro", f"{tmp}/pop.wav", "-o", f"{tmp}/sfx.mp4", "--spacing", "1")
d2 = float(run("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", f"{tmp}/sfx.mp4"))
check("효과음 믹스 영상 생성", abs(d2 - d) < 0.3)

print("\n실패:", fails if fails else "없음")
sys.exit(1 if fails else 0)
