# 무드보드 빌더 (AI 출처 검증)

`bx-agent-workflow`의 아티팩트 버전과는 완전히 별개인 독립 웹앱입니다. 차이는 딱 하나: 이 앱은 서버(`api/verify.py`)가 있어서, "AI로 확인" 버튼을 누르면 그 서버가 Gemini API(구글 검색 도구 포함)를 실제로 호출해 이미지 출처를 그 자리에서 검증합니다. 아티팩트 버전은 이게 구조적으로 불가능해서(브라우저 안에서 외부 API 호출 자체가 막혀 있음) PDF 내보내기 같은 우회 방법을 썼는데, 여기서는 필요 없습니다.

**비용 관련 결정**: 처음엔 Anthropic(Claude) API로 만들었는데, 테스트 몇 번에 크레딧이 빠르게 소진돼서(도구 사용 한도를 너무 넓게 잡았던 실수 + 유료 API라는 근본적 한계) **Google Gemini API로 교체했습니다(2026-09-02)**. `gemini-2.5-flash` 모델은 이미지 인식 + 구글 검색 연동(Grounding)까지 **하루 500회 무료**로 제공돼서, 이 프로젝트 사용량이면 사실상 계속 무료로 쓸 수 있습니다. *주의: `gemini-3.x` 계열 모델은 무료 등급에서 검색 연동이 안 됨(Google AI Studio에서만 테스트 가능) — 모델을 바꾸기 전에 반드시 https://ai.google.dev/gemini-api/docs/pricing 에서 무료 등급에 검색 연동이 포함되는지 다시 확인할 것.*

## 구조
- `index.html` — 무드보드 빌더 전체(레이아웃 엔진, PPT 내보내기, 출처 리포트). 순수 정적 파일, 빌드 단계 없음.
- `api/verify.py` — Vercel Python 서버리스 함수. `POST /api/verify`로 이미지(dataURI)를 받아 (있으면) 구글 비전 API로 후보 출처를 먼저 찾고, Gemini API(구글 검색 도구 포함)로 실제로 맞는지 검증한 결과 JSON을 돌려줌.
- `api/requirements.txt` — 서버 함수용 파이썬 의존성(`google-genai` SDK만, 구글 비전 호출은 표준 라이브러리 `urllib`만 씀).
- `vercel.json` — 정적 파일(`index.html`)과 파이썬 함수(`api/verify.py`)를 명시적으로 분리해서 빌드하는 설정(`builds`/`routes`). *주의: 여기에 `pyproject.toml`을 다시 추가하거나 `functions` 키로 바꾸지 말 것 — 둘 다 Vercel이 사이트 전체를 파이썬 앱으로 오인해서 정적 파일 라우팅이 깨지는 걸 실제로 겪었음(2026-09-01).*

이미지·검증 결과는 전부 **브라우저 localStorage**에만 저장됩니다. 여러 명이 같은 링크를 열어도 서로의 무드보드나 검증 결과는 절대 섞이지 않습니다(각자 자기 브라우저 것만 봄).

## 회사 와이파이에서 API가 막히는 문제
API 키는 Vercel 서버 환경변수로만 존재하고, 브라우저는 그 키를 절대 보지 않습니다. 브라우저 → 우리 서버는 일반 웹사이트 방문과 동일한 트래픽이라 회사 방화벽에 걸릴 일이 거의 없고, 서버 → Google API 호출은 사용자 네트워크를 아예 거치지 않습니다(Vercel 클라우드에서 나가는 요청). 즉 한 번 배포해두면 계속 켜져 있을 필요 없이 항상 이 상태로 동작합니다.

## 배포 방법 (Vercel, CLI 설치 없이)

이 컴퓨터에 Node.js/npm/Vercel CLI가 설치되어 있지 않아서, GitHub 저장소를 거쳐 Vercel 대시보드에서 연결하는 방식으로 안내합니다(전부 클릭 몇 번).

1. **Gemini API 키 발급 (무료)**: [aistudio.google.com](https://aistudio.google.com) → 구글 계정으로 로그인 → "Get API key" → 새 키 생성. 결제 정보 등록 없이 바로 발급됩니다.
2. **GitHub에 빈 저장소 하나 생성**: github.com → New repository → 이름은 아무거나 → **Public/Private 상관없음** → 아무 파일도 추가하지 말고(README 체크 해제) 생성만.
3. 저한테 그 저장소 URL을 알려주시면, 제가 지금 이 코드를 그 저장소로 push해드릴게요.
4. **Vercel 연결**: [vercel.com](https://vercel.com) → GitHub 계정으로 로그인 → "Add New... → Project" → 방금 만든 저장소 선택 → Import.
5. Import 화면에서 "Environment Variables"에 `GOOGLE_API_KEY` = (1번에서 받은 키) 추가 → Deploy. *(변수 이름이 정확히 `GOOGLE_API_KEY`여야 SDK가 자동으로 읽습니다.)*
6. 배포되면 Vercel이 `https://<프로젝트명>.vercel.app` 같은 URL을 줍니다. 그게 팀원들과 공유할 링크예요.

이후 코드를 수정하고 싶으면 저한테 다시 요청하시면 되고, 제가 같은 저장소에 push하면 Vercel이 자동으로 재배포합니다(GitHub 연동 시 기본 동작).

### (선택) 구글 비전 API로 정확도 올리기 — Gemini API 키와는 별개로 하나 더 필요함
텍스트 검색만으로는 로고·간판 같은 단서가 없는 사진을 못 찾을 때가 있어서, 구글 Cloud Vision의 Web Detection(실제 픽셀 기반 역이미지검색)을 먼저 돌려 후보 링크를 찾고 Gemini가 그걸 확인하도록 붙일 수 있습니다.

**Gemini API 키(1번)와 이 구글 비전 키는 서로 다른 발급 절차예요** — 둘 다 구글 계정으로 하지만, Gemini는 AI Studio에서 원클릭으로 발급되고, 비전 API는 정식 Google Cloud 프로젝트를 만들고 그 안에서 API를 켠 다음 별도로 키를 발급받아야 합니다.

1. [console.cloud.google.com](https://console.cloud.google.com) → 새 프로젝트 → "Vision API" 검색해서 Cloud Vision API "사용(Enable)"
2. API 및 서비스 → 사용자 인증 정보 → API 키 생성
3. Vercel 환경변수에 `GOOGLE_VISION_API_KEY` 추가 (이름이 `GOOGLE_API_KEY`와 다름에 주의)

이 환경변수가 없으면 그냥 기존 방식(Gemini 자체 구글 검색)으로만 동작합니다 — 필수 아님. 월 1,000건까지 무료, 이후 1,000건당 $3.50.

## 비용
`gemini-2.5-flash`는 이미지 인식과 구글 검색 연동 모두 **하루 500회까지 무료**입니다(과금 없음). 이 무료 한도를 넘기지 않는 한 이 앱은 완전히 무료로 운영됩니다. 무료 등급에서는 구글이 요청/응답 데이터를 자체 모델 학습에 사용할 수 있다는 점만 참고해주세요(결제를 켜면 이 데이터 사용을 끌 수 있지만, 그러면 유료 전환이라는 뜻이라 지금은 켜지 않았습니다).
