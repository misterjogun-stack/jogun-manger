#!/usr/bin/env python3
"""말 영상 자동 편집기: 무음 컷, 재촬영 삭제, 넷플릭스식 한국어 자막, 효과음 믹스.

필요한 것: ffmpeg, ffprobe, Python 3.9+.  받아 적기(transcribe)에만 faster-whisper 또는 whisper.cpp 가 필요하다.

  python autoedit.py transcribe raw.mp4 -o raw.json
  python autoedit.py edit raw.mp4 --transcript raw.json --out out
  python autoedit.py all raw.mp4 --out out
  python autoedit.py sfx out/edited.mp4 --report out/report.json --cut whoosh.wav -o out/edited_sfx.mp4
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from difflib import SequenceMatcher

HERE = os.path.dirname(os.path.abspath(__file__))


def sh(cmd, check=True, capture=True):
    r = subprocess.run(cmd, capture_output=capture, text=True)
    if check and r.returncode != 0:
        sys.exit("명령 실패: " + " ".join(cmd[:6]) + " ...\n" + (r.stderr or "")[-1500:])
    return r


def duration(path):
    r = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path])
    return float(r.stdout.strip())


def has_audio(path):
    r = sh(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type", "-of", "csv=p=0", path])
    return bool(r.stdout.strip())


# ---------------------------------------------------------------- 무음 감지
def speech_intervals(path, dur, noise_db, min_sil):
    """ffmpeg silencedetect 의 반대: 소리가 있는 구간 목록."""
    if not has_audio(path):
        return [(0.0, dur)]
    r = sh(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af",
            f"silencedetect=noise={noise_db}dB:d={min_sil}", "-f", "null", "-"])
    sil, start = [], None
    for line in r.stderr.splitlines():
        m = re.search(r"silence_start:\s*(-?[\d.]+)", line)
        if m:
            start = max(0.0, float(m.group(1)))
        m = re.search(r"silence_end:\s*([\d.]+)", line)
        if m and start is not None:
            sil.append((start, float(m.group(1))))
            start = None
    if start is not None:
        sil.append((start, dur))
    out, t = [], 0.0
    for a, b in sil:
        if a - t > 0.05:
            out.append((t, a))
        t = max(t, b)
    if dur - t > 0.05:
        out.append((t, dur))
    return out


# ---------------------------------------------------------------- 구간 계산
def merge(iv, gap=0.0):
    iv = sorted(iv)
    out = []
    for a, b in iv:
        if out and a <= out[-1][1] + gap:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def subtract(iv, cuts):
    out = []
    for a, b in iv:
        segs = [(a, b)]
        for c, d in cuts:
            nxt = []
            for x, y in segs:
                if d <= x or c >= y:
                    nxt.append((x, y))
                else:
                    if c > x:
                        nxt.append((x, c))
                    if d < y:
                        nxt.append((d, y))
            segs = nxt
        out += segs
    return out


# ---------------------------------------------------------------- 받아쓰기 읽기
def load_transcript(path):
    """우리 형식, faster-whisper 형식, whisper.cpp -oj 형식을 같은 모양으로 맞춘다."""
    data = json.load(open(path, encoding="utf-8"))
    segs = []
    if "segments" in data:
        for s in data["segments"]:
            words = [{"w": (w.get("word") or w.get("w") or "").strip(), "s": float(w["start"]), "e": float(w["end"])}
                     for w in s.get("words", []) if (w.get("word") or w.get("w") or "").strip()]
            segs.append({"s": float(s["start"]), "e": float(s["end"]), "text": s["text"].strip(), "words": words})
    elif "transcription" in data:
        for s in data["transcription"]:
            off = s["offsets"]
            segs.append({"s": off["from"] / 1000, "e": off["to"] / 1000, "text": s["text"].strip(), "words": []})
    else:
        sys.exit("알 수 없는 받아쓰기 JSON 형식입니다.")
    return [s for s in segs if s["text"]]


def norm(t):
    return re.sub(r"[^0-9A-Za-z가-힣]", "", t).lower()


def to_units(segs, retake_gap):
    """문장 안에서도 멈춤(retake_gap 이상)이 있으면 쪼개서 '테이크' 단위로 만든다."""
    units = []
    for s in segs:
        w = s["words"]
        if not w:
            units.append({"s": s["s"], "e": s["e"], "text": s["text"], "words": []})
            continue
        chunk = [w[0]]
        for prev, cur in zip(w, w[1:]):
            if cur["s"] - prev["e"] >= retake_gap:
                units.append({"s": chunk[0]["s"], "e": chunk[-1]["e"], "text": " ".join(x["w"] for x in chunk), "words": chunk})
                chunk = []
            chunk.append(cur)
        units.append({"s": chunk[0]["s"], "e": chunk[-1]["e"], "text": " ".join(x["w"] for x in chunk), "words": chunk})
    return units


def is_redo(na, nb, ratio):
    """앞 테이크(na)가 뒤 테이크(nb)의 다시 말하기인가."""
    if len(na) < 3 or len(nb) < 3:
        return False
    if nb.startswith(na):                       # 말하다 끊김: 앞이 뒤의 앞부분
        return True
    cp = 0
    for x, y in zip(na, nb):
        if x != y:
            break
        cp += 1
    if cp >= max(3, 0.6 * len(na)):             # 앞 테이크 대부분이 뒤에서 반복
        return True
    return SequenceMatcher(None, na, nb).ratio() >= ratio


def find_retakes(units, retake_gap, window, ratio):
    """{버릴 단위 index: 살릴 단위 index}. 멈춤 뒤에 같은 말이 다시 나오면 앞부분을 버린다."""
    drop = {}
    for i, a in enumerate(units):
        na = norm(a["text"])
        for j in range(i + 1, min(len(units), i + 1 + window)):
            b = units[j]
            if b["s"] - a["e"] < retake_gap:
                continue
            if is_redo(na, norm(b["text"]), ratio):
                drop[i] = j
                break
    return drop


# ---------------------------------------------------------------- 편집 계획
def plan_edit(video, transcript, a, cfg):
    dur = duration(video)
    sp = speech_intervals(video, dur, a.noise, a.min_silence)
    units, drop, drop_win = [], {}, []
    if transcript:
        units = to_units(transcript, a.retake_gap)
        drop = find_retakes(units, a.retake_gap, a.window, a.ratio)
        for i in drop:
            u = units[i]
            nxt = units[i + 1]["s"] if i + 1 < len(units) else dur
            drop_win.append((max(0.0, u["s"] - 0.05), min(u["e"] + 0.15, nxt - 0.05)))
    sp = [(x, y) for x, y in sp if y - x >= a.min_speech]
    padded = [(max(0.0, x - a.pad), min(dur, y + a.pad)) for x, y in sp]
    keeps = merge(subtract(merge(padded, a.join_gap), drop_win), 0.0)
    keeps = [(x, y) for x, y in keeps if y - x >= 0.2]
    if not keeps:
        sys.exit("남은 구간이 없습니다. --noise 값을 -45 처럼 낮춰 보세요.")
    starts, t = [], 0.0
    for x, y in keeps:
        starts.append(t)
        t += y - x

    def to_new(old):
        for (x, y), ns in zip(keeps, starts):
            if x - 1e-6 <= old <= y + 1e-6:
                return ns + (old - x)
        return None

    removed_retake = sum(w[1] - w[0] for w in drop_win)
    cues = []
    prev_end = 0.0
    for k, (x, y) in enumerate(keeps):
        if k == 0:
            cues.append({"t": 0.0, "kind": "intro", "removed": 0.0})
        else:
            removed = x - keeps[k - 1][1]
            cues.append({"t": round(starts[k], 3), "kind": "cut", "removed": round(removed, 3)})
    words = []
    for i, u in enumerate(units):
        if i in drop:
            continue
        ws = u["words"] or spread_words(u)
        for w in ws:
            m = to_new((w["s"] + w["e"]) / 2)
            if m is None:
                continue
            ln = w["e"] - w["s"]
            words.append({"w": w["w"], "s": max(0.0, m - ln / 2), "e": m + ln / 2})
    words.sort(key=lambda z: z["s"])
    retakes = [{"dropped": units[i]["text"], "at": round(units[i]["s"], 2), "kept": units[j]["text"], "kept_at": round(units[j]["s"], 2)}
               for i, j in sorted(drop.items())]
    return {"duration": dur, "keeps": keeps, "starts": starts, "out_duration": t, "cues": cues,
            "words": words, "retakes": retakes, "removed_retake": removed_retake}


def spread_words(u):
    """단어 시간이 없는 받아쓰기: 글자 수에 비례해 나눈다."""
    ws = u["text"].split()
    tot = sum(len(w) for w in ws) or 1
    out, t = [], u["s"]
    for w in ws:
        d = (u["e"] - u["s"]) * len(w) / tot
        out.append({"w": w, "s": t, "e": t + d})
        t += d
    return out


# ---------------------------------------------------------------- 넷플릭스식 자막
def cw(ch, cfg):
    if ch == " ":
        return 0.5
    if "가" <= ch <= "힣" or "ㄱ" <= ch <= "ㅣ" or "一" <= ch <= "鿿":
        return 1.0
    if ch.isdigit():
        return cfg["digit_weight"]
    return 0.5


def wt(text, cfg):
    return sum(cw(c, cfg) for c in text)


def wrap(words, cfg):
    """words(문자열 목록) → 줄 목록(최대 max_lines, 줄당 line_max). 안 되면 None."""
    full = " ".join(words)
    if wt(full, cfg) <= cfg["line_max"]:
        return [full]
    if cfg["max_lines"] < 2:
        return None
    best = None
    for k in range(1, len(words)):
        l1, l2 = " ".join(words[:k]), " ".join(words[k:])
        if wt(l1, cfg) <= cfg["line_max"] and wt(l2, cfg) <= cfg["line_max"]:
            d = abs(wt(l1, cfg) - wt(l2, cfg))
            if best is None or d < best[0]:
                best = (d, [l1, l2])
    return best[1] if best else None


def make_cues(words, cfg):
    cues, cur = [], []

    def flush():
        nonlocal cur
        if cur:
            lines = wrap([w["w"] for w in cur], cfg) or [" ".join(w["w"] for w in cur)]
            cues.append({"s": cur[0]["s"], "e": cur[-1]["e"], "lines": lines, "forced": wrap([w["w"] for w in cur], cfg) is None})
        cur = []

    for w in words:
        if cur:
            gap = w["s"] - cur[-1]["e"]
            ends = bool(re.search(r"[.?!…,]$", cur[-1]["w"]))
            too_long = wrap([x["w"] for x in cur] + [w["w"]], cfg) is None
            if gap > cfg["pause_split"] or too_long or (w["e"] - cur[0]["s"]) > cfg["max_dur"] \
                    or (ends and wt(" ".join(x["w"] for x in cur), cfg) >= cfg["line_max"] * 0.6):
                flush()
        cur.append(w)
    flush()

    for c in cues:
        out = []
        for ln in c["lines"]:
            if cfg["strip_trailing_period"]:
                ln = re.sub(r"\.+$", "", ln)
            if cfg["strip_trailing_comma"]:
                ln = re.sub(r",+$", "", ln)
            out.append(ln.strip())
        c["lines"] = [x for x in out if x]
    cues = [c for c in cues if c["lines"]]

    warnings = []
    for i, c in enumerate(cues):
        chars = sum(cw(ch, cfg) for ln in c["lines"] for ch in ln if ch != " ")
        need = max(chars / cfg["cps"], cfg["min_dur"])
        limit = c["s"] + cfg["max_dur"]
        if i + 1 < len(cues):
            limit = min(limit, cues[i + 1]["s"] - cfg["min_gap"])
        c["e"] = max(c["e"], min(c["s"] + need, limit))
        if c["e"] - c["s"] + 1e-6 < chars / cfg["cps"]:
            warnings.append({"at": round(c["s"], 2), "problem": "읽는 속도 초과", "cps": round(chars / max(c["e"] - c["s"], 0.01), 1), "text": " / ".join(c["lines"])})
        if c["forced"] or any(wt(ln, cfg) > cfg["line_max"] for ln in c["lines"]):
            warnings.append({"at": round(c["s"], 2), "problem": f"줄 길이 {cfg['line_max']}자 초과", "text": " / ".join(c["lines"])})
        if any(wt(ln, cfg) > cfg["qc_hard_line_max"] for ln in c["lines"]):
            warnings.append({"at": round(c["s"], 2), "problem": "넷플릭스 검수 한도 초과", "text": " / ".join(c["lines"])})
    return cues, warnings


def srt_time(t):
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def write_srt(cues, path):
    with open(path, "w", encoding="utf-8") as f:
        for i, c in enumerate(cues, 1):
            f.write(f"{i}\n{srt_time(c['s'])} --> {srt_time(c['e'])}\n" + "\n".join(c["lines"]) + "\n\n")


# ---------------------------------------------------------------- 렌더
def render(video, keeps, out, crf):
    audio = has_audio(video)
    parts = []
    for i, (x, y) in enumerate(keeps):
        parts.append(f"[0:v]trim=start={x:.3f}:end={y:.3f},setpts=PTS-STARTPTS[v{i}];")
        if audio:
            f = min(0.015, (y - x) / 4)
            parts.append(f"[0:a]atrim=start={x:.3f}:end={y:.3f},asetpts=PTS-STARTPTS,afade=t=in:d={f:.3f},afade=t=out:st={y - x - f:.3f}:d={f:.3f}[a{i}];")
    n = len(keeps)
    ins = "".join(f"[v{i}][a{i}]" if audio else f"[v{i}]" for i in range(n))
    parts.append(f"{ins}concat=n={n}:v=1:a={1 if audio else 0}[v]" + ("[a]" if audio else ""))
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write("".join(parts))
        script = fh.name
    cmd = ["ffmpeg", "-hide_banner", "-y", "-i", video, "-filter_complex_script", script, "-map", "[v]"]
    if audio:
        cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-c:v", "libx264", "-crf", str(crf), "-preset", "medium", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]
    sh(cmd)
    os.unlink(script)


# ---------------------------------------------------------------- 명령
def cmd_edit(a):
    cfg = json.load(open(a.config, encoding="utf-8"))
    transcript = load_transcript(a.transcript) if a.transcript else None
    os.makedirs(a.out, exist_ok=True)
    p = plan_edit(a.video, transcript, a, cfg)
    cues, warn = make_cues(p["words"], cfg) if transcript else ([], [])
    if transcript:
        write_srt(cues, os.path.join(a.out, "edited.srt"))
    removed_total = p["duration"] - p["out_duration"]
    report = {
        "input": a.video, "input_duration": round(p["duration"], 2), "output_duration": round(p["out_duration"], 2),
        "removed_seconds": round(removed_total, 2), "removed_in_retakes": round(p["removed_retake"], 2),
        "kept_segments": len(p["keeps"]), "retakes": p["retakes"],
        "subtitle_warnings": warn, "subtitle_count": len(cues),
        "sfx_cues": p["cues"],
        "keeps": [[round(x, 3), round(y, 3)] for x, y in p["keeps"]],
        "settings": {"noise_db": a.noise, "min_silence": a.min_silence, "pad": a.pad, "retake_gap": a.retake_gap},
    }
    json.dump(report, open(os.path.join(a.out, "report.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    if not a.dry_run:
        render(a.video, p["keeps"], os.path.join(a.out, "edited.mp4"), a.crf)
    print(f"{p['duration']:.1f}초 → {p['out_duration']:.1f}초 (잘라낸 {removed_total:.1f}초, 재촬영 {len(p['retakes'])}곳, 자막 {len(cues)}개, 경고 {len(warn)}개)")
    for r in p["retakes"]:
        print(f"  재촬영 삭제 {r['at']}s \"{r['dropped']}\"  →  남긴 말 {r['kept_at']}s \"{r['kept']}\"")
    for w in warn[:8]:
        print(f"  자막 경고 {w['at']}s {w['problem']}: {w['text']}")
    print(f"결과: {a.out}/ (edited.mp4{'' if not a.dry_run else ' 생략(dry-run)'}, edited.srt, report.json)")


def cmd_transcribe(a):
    out = a.output or os.path.splitext(a.video)[0] + ".json"
    wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    sh(["ffmpeg", "-hide_banner", "-y", "-i", a.video, "-vn", "-ac", "1", "-ar", "16000", wav])
    engine = a.engine
    if engine == "auto":
        try:
            import faster_whisper  # noqa: F401
            engine = "faster"
        except ImportError:
            engine = "cpp"
    if engine == "faster":
        from faster_whisper import WhisperModel
        # int8: 성능 낮은 컴퓨터에서도 large-v3-turbo 를 돌리기 위한 설정
        model = WhisperModel(a.model, device="auto", compute_type=a.compute_type)
        segs, _ = model.transcribe(wav, language=a.lang, word_timestamps=True, vad_filter=False,
                                   condition_on_previous_text=False, beam_size=1)
        data = {"segments": [{"start": s.start, "end": s.end, "text": s.text,
                              "words": [{"word": w.word, "start": w.start, "end": w.end} for w in (s.words or [])]} for s in segs]}
        json.dump(data, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    else:
        if not a.cpp_model:
            sys.exit("whisper.cpp 를 쓰려면 --cpp-model ggml-large-v3-turbo-q5_0.bin 경로를 주세요.")
        base = os.path.splitext(out)[0]
        sh(["whisper-cli", "-m", a.cpp_model, "-f", wav, "-l", a.lang, "-oj", "-of", base], capture=True)
        os.replace(base + ".json", out)
    os.unlink(wav)
    print("받아쓰기 저장:", out)
    return out


def cmd_all(a):
    a.output = os.path.join(a.out, "transcript.json")
    os.makedirs(a.out, exist_ok=True)
    a.transcript = cmd_transcribe(a)
    cmd_edit(a)


def cmd_sfx(a):
    rep = json.load(open(a.report, encoding="utf-8"))
    cues = [c for c in rep["sfx_cues"] if (c["kind"] == "cut" and a.cut and c["removed"] >= a.min_removed) or (c["kind"] == "intro" and a.intro)]
    picked, last = [], -99.0
    for c in cues:
        if c["kind"] == "intro" or c["t"] - last >= a.spacing:
            picked.append(c)
            last = c["t"]
    if not picked:
        sys.exit("넣을 효과음 위치가 없습니다. --min-removed 를 낮추거나 --spacing 을 줄여 보세요.")
    ins = ["-i", a.video]
    sources = {}
    for kind, path in (("cut", a.cut), ("intro", a.intro)):
        if path:
            sources[kind] = len(sources) + 1
            ins += ["-i", path]
    filt, labels = [], ["[0:a]"]
    for i, c in enumerate(picked):
        src = sources.get(c["kind"])
        if src is None:
            continue
        ms = int(c["t"] * 1000)
        filt.append(f"[{src}:a]volume={a.gain_db}dB,adelay={ms}|{ms}[s{i}]")
        labels.append(f"[s{i}]")
    filt.append("".join(labels) + f"amix=inputs={len(labels)}:duration=first:normalize=0[a]")
    cmd = ["ffmpeg", "-hide_banner", "-y"] + ins + ["-filter_complex", ";".join(filt), "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", a.output]
    sh(cmd)
    print(f"효과음 {len(labels) - 1}곳 믹스 → {a.output}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def edit_opts(p):
        p.add_argument("--out", default="out")
        p.add_argument("--config", default=os.path.join(HERE, "netflix_ko.json"))
        p.add_argument("--noise", type=float, default=-35, help="이 dB 아래는 무음 (기본 -35)")
        p.add_argument("--min-silence", type=float, default=0.5, help="이보다 긴 무음만 자름(초)")
        p.add_argument("--min-speech", type=float, default=0.15)
        p.add_argument("--pad", type=float, default=0.12, help="말 앞뒤로 남기는 여유(초)")
        p.add_argument("--join-gap", type=float, default=0.25, help="이보다 짧은 간격은 이어 붙임")
        p.add_argument("--retake-gap", type=float, default=0.6, help="이 시간 이상 멈춘 뒤 같은 말이 나오면 재촬영으로 봄")
        p.add_argument("--window", type=int, default=3, help="뒤따르는 몇 개 테이크까지 비교할지")
        p.add_argument("--ratio", type=float, default=0.75, help="비슷한 정도(0~1)")
        p.add_argument("--crf", type=int, default=20)
        p.add_argument("--dry-run", action="store_true", help="영상은 만들지 않고 계획과 자막만 만든다")

    def tr_opts(p):
        p.add_argument("--model", default="large-v3-turbo")
        p.add_argument("--lang", default="ko")
        p.add_argument("--engine", choices=["auto", "faster", "cpp"], default="auto")
        p.add_argument("--compute-type", default="int8")
        p.add_argument("--cpp-model")

    t = sub.add_parser("transcribe"); t.add_argument("video"); t.add_argument("-o", "--output"); tr_opts(t); t.set_defaults(fn=cmd_transcribe)
    e = sub.add_parser("edit"); e.add_argument("video"); e.add_argument("--transcript"); edit_opts(e); e.set_defaults(fn=cmd_edit)
    al = sub.add_parser("all"); al.add_argument("video"); edit_opts(al); tr_opts(al); al.set_defaults(fn=cmd_all)
    s = sub.add_parser("sfx"); s.add_argument("video"); s.add_argument("--report", required=True)
    s.add_argument("--cut", help="컷 이음새에 넣을 효과음 wav"); s.add_argument("--intro", help="맨 처음에 넣을 효과음 wav")
    s.add_argument("--gain-db", type=float, default=-16); s.add_argument("--min-removed", type=float, default=0.8)
    s.add_argument("--spacing", type=float, default=4.0, help="효과음 사이 최소 간격(초)"); s.add_argument("-o", "--output", required=True)
    s.set_defaults(fn=cmd_sfx)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
