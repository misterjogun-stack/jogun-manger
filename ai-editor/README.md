# AI 영상 자동 편집 (Claude Code용)

말하는 영상을 올리면 **무음 컷 → 재촬영 삭제 → 넷플릭스식 자막 → 효과음**까지 한 번에 처리하는 도구와, Claude Code에 그대로 붙여 넣을 프롬프트 모음이에요.

## 한눈에 보기

| 단계 | 하는 일 | 어디서 |
|---|---|---|
| 1. 촬영 | 말 실수하면 **1초 이상 멈췄다가 문장을 처음부터** 다시 말하기 | 촬영할 때 |
| 2. 받아쓰기 | Whisper `large-v3-turbo`로 단어별 시간 받아쓰기 | `autoedit.py transcribe` |
| 3. 컷 | 무음 자르기, 반복해서 말한 부분은 **앞을 버리고 뒤를 살림** | `autoedit.py edit` |
| 4. 자막 | 넷플릭스 한국어 자막 지침(줄당 16자, 최대 2줄, 초당 12자)에 맞춰 SRT 생성 | `autoedit.py edit` |
| 5. 모션그래픽 | HyperFrames(HTML로 영상 만들기)로 타이틀·강조 화면 | Claude Code + HyperFrames |
| 6. 효과음 | earshot으로 효과음 만들고 컷 이음새에 믹스 | `autoedit.py sfx` |

## 1. 촬영 규칙 (제일 중요)

AI는 **멈춤**으로 재촬영을 알아봐요.

1. 말이 꼬이면 말을 멈추고 **1초 이상 조용히 있다가**
2. **그 문장의 처음부터** 다시 말하세요. 중간부터 이어 말하면 앞부분을 못 찾아요.
3. 같은 말을 여러 번 해도 돼요. **마지막 테이크가 살아요.**

## 2. 설치 (내 컴퓨터 또는 Claude Code 클라우드 환경)

```bash
# 필수
sudo apt install ffmpeg            # 맥은 brew install ffmpeg
pip install faster-whisper         # 받아쓰기 (large-v3-turbo, int8 모드라 사양이 낮아도 돌아가요)

# 컴퓨터가 많이 느리면 whisper.cpp 와 양자화 모델(약 574MB)도 쓸 수 있어요
# 모델: ggml-large-v3-turbo-q5_0.bin  →  python autoedit.py transcribe raw.mp4 --engine cpp --cpp-model <모델 경로>

# 5단계: HyperFrames (Node 22 이상 필요)
npx skills add heygen-com/hyperframes

# 6단계: earshot (효과음)
# Claude Code에게 "Leonxlnx/earshot 을 설치하고 README대로 sfx를 만들어 줘"라고 시키세요.
```

> Claude Code **클라우드(웹)** 환경에서 쓰려면 환경 설정에서 `huggingface.co`(모델 내려받기), `github.com`(earshot) 접속을 허용해야 해요. 이 도구를 만든 환경은 막혀 있어서 받아쓰기 모델은 직접 내려받아 실행해 보지 못했어요.

## 3. 한 번에 실행

```bash
python autoedit.py all raw.mp4 --out out
```

`out/` 에 이게 생겨요.

- `edited.mp4` 편집된 영상
- `edited.srt` 넷플릭스식 자막 (유튜브 업로드 때 자막 파일로 올리세요)
- `report.json` 무엇을 잘랐는지 기록. **재촬영으로 지운 문장 목록(`retakes`)과 자막 경고(`subtitle_warnings`)를 꼭 확인하세요.**
- `transcript.json` 받아쓰기 원본

받아쓰기를 이미 했으면 `python autoedit.py edit raw.mp4 --transcript transcript.json --out out`.
영상은 만들지 않고 계획만 보려면 `--dry-run`을 붙여요.

### 자주 쓰는 옵션

| 옵션 | 기본값 | 쓰임새 |
|---|---|---|
| `--noise` | -35 | 배경 소음이 크면 -30, 아주 조용하면 -45 |
| `--min-silence` | 0.5 | 이보다 긴 침묵만 자름. 템포를 더 빠르게 하려면 0.3 |
| `--pad` | 0.12 | 말 앞뒤로 남기는 여유(초). 말이 잘리면 늘리세요 |
| `--retake-gap` | 0.6 | 이 시간 이상 멈춘 뒤 같은 말이 나오면 재촬영으로 판단 |
| `--ratio` | 0.75 | 재촬영 판단 기준. 멀쩡한 문장이 지워지면 0.85로 올리세요 |

## 4. Claude Code에 붙여 넣을 프롬프트

폴더에 `CLAUDE.md`가 있어서 Claude Code가 규칙을 이미 알고 있어요. 아래를 순서대로 붙여 넣으세요.

**① 준비**
```
ai-editor/README.md를 읽고 faster-whisper와 ffmpeg가 설치돼 있는지 확인해 줘. 없으면 설치해 줘. 모델은 large-v3-turbo를 int8로 쓸 거야.
```

**② 컷 편집**
```
raw.mp4를 python autoedit.py all raw.mp4 --out out 으로 편집해 줘. 끝나면 out/report.json의 retakes를 보여 주고, 멀쩡한 문장이 지워진 건 없는지 같이 확인해 줘. 있으면 --ratio를 조정해서 다시 돌려 줘.
```

**③ 자막 (넷플릭스 지침 대조)**
```
https://backlothelp.netflix.com/hc/en-us/articles/216001127 (넷플릭스 한국어 자막 지침)을 읽고, ai-editor/netflix_ko.json의 값과 하나씩 대조해 줘. 다른 값이나 verified가 false인 항목은 지침 원문대로 고치고, 고친 항목을 알려 줘. 그다음 edit를 다시 돌려 out/edited.srt를 만들어 줘. subtitle_warnings가 있으면 문장을 줄이거나 나눠서 해결해 줘.
```

**④ 모션그래픽 (HyperFrames)**
```
HyperFrames로 3초짜리 인트로 타이틀과 핵심 단어 강조 화면을 만들어 줘. 제목은 "<제목>". 렌더한 뒤 ffmpeg overlay로 out/edited.mp4 위에 얹어 줘. 리모션은 쓰지 마.
```

**⑤ 효과음 (earshot)**
```
Leonxlnx/earshot을 설치해서 컷 이음새용 짧은 whoosh 효과음과 인트로용 효과음을 wav로 만들어 줘. 소리가 작고 부드러워서 말소리를 가리지 않는지 직접 들어보고 평가해 줘. 그다음 python autoedit.py sfx out/edited.mp4 --report out/report.json --cut <컷 효과음.wav> --intro <인트로 효과음.wav> -o out/final.mp4 로 믹스해 줘.
```

## 5. 이 도구가 하는 일과 한계

**테스트한 것** (`python tests/run_test.py`, 가짜 말소리와 가짜 받아쓰기 사용)
- 무음 감지와 컷, 컷 사이 소리 끊김 방지(짧은 페이드)
- 끊긴 첫 시도 삭제, 한 문장 안에서 멈춘 뒤 다시 시작한 부분 삭제, 완성된 말은 보존
- 자막: 줄당 16자 이하(라틴·공백·문장부호 0.5자), 최대 2줄, 초당 12자 읽기 속도 확인, 끝 마침표 제거, 컷으로 달라진 시간 반영
- 렌더된 영상 길이가 계획과 일치, 효과음 믹스

**테스트하지 못한 것**
- 실제 Whisper 받아쓰기(모델 내려받기가 막혀 있었어요). `faster-whisper` 1.2.1에 `large-v3-turbo` 이름과 단어 시간 옵션이 있는 것만 코드로 확인했어요. `whisper.cpp` 경로는 실행해 보지 못했어요.
- 실제 말소리 영상에서의 재촬영 판단 정확도. 판단은 **멈춤 + 앞부분이 뒤 테이크와 비슷함**이라는 규칙이라, 일부러 같은 말을 반복하는 연설체에서는 잘못 지울 수 있어요. 그래서 `report.json`의 `retakes`를 꼭 확인하세요.
- earshot, HyperFrames 실제 설치와 사용(문서로만 확인).
- 넷플릭스 지침 원문. 접속이 막혀서 읽지 못했어요. 줄당 16자, 초당 12자, 라틴·공백·문장부호 0.5자는 검색 결과로 확인한 값이고, 최대 2줄, 최소·최대 표시 시간, 끝 마침표 제거는 확인하지 못한 값이에요(`netflix_ko.json`에 `verified: false`). ③ 프롬프트로 원문과 대조하세요.

원 글의 "주 5회 업로드", "한 달 조회수 19만" 같은 성과는 글쓴이의 주장이고, 같은 결과가 나온다는 보장은 없어요.

## 참고

- [넷플릭스 한국어 자막 지침](https://backlothelp.netflix.com/hc/en-us/articles/216001127)
- [heygen-com/hyperframes](https://github.com/heygen-com/hyperframes): 라이선스는 설치 전에 저장소의 LICENSE로 확인하세요(미러마다 설명이 달라요).
- [Leonxlnx/earshot](https://github.com/Leonxlnx/earshot): 효과음을 코드로 만들고 에이전트가 들어 보며 고치는 도구. 소리가 좋다는 점수와 실제로 좋게 들리는 것은 다를 수 있어서 직접 들어 보라고 안내돼 있어요.
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
