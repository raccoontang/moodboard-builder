# 무드보드 빌더 (AI 출처 검증)

`bx-agent-workflow`의 아티팩트 버전과는 완전히 별개인 독립 웹앱입니다. 차이는 딱 하나: 이 앱은 서버(`api/verify.py`)가 있어서, "AI로 확인" 버튼을 누르면 그 서버가 Gemini API + 구글 비전 API를 실제로 호출해 이미지 출처를 그 자리에서 검증합니다. 아티팩트 버전은 이게 구조적으로 불가능해서(브라우저 안에서 외부 API 호출 자체가 막혀 있음) PDF 내보내기 같은 우회 방법을 썼는데, 여기서는 필요 없습니다.

## 비용/구조 결정 히스토리 (2026-09-02) — 왜 이렇게 생겼는지
1. 처음엔 Anthropic(Claude) API로 만들었는데, 테스트 몇 번에 크레딧이 빠르게 소진돼서(도구 사용 한도를 너무 넓게 잡은 실수 + 유료 API라는 근본적 한계) **Google Gemini API로 교체**.
2. 처음 계획은 Gemini의 `google_search` 도구(무료 검색 연동)로 직접 검색하는 거였는데, **실제로 배포해서 테스트해보니** (문서만 보고 판단하지 않고):
   - `gemini-2.5-flash`(무료 검색 연동이 있던 모델)는 **신규 사용자에게 아예 막혀있음**(404, "no longer available to new users").
   - 대체 모델 `gemini-3.6-flash`는 `google_search` 도구를 쓰는 즉시 **429(할당량 초과)** — 이 모델엔 무료 검색 연동이 없음.
   - 반면 **`url_context` 도구(이미 아는 특정 URL을 열어서 읽기)는 별도 할당량이라 무료로 잘 됨** — 실제 테스트로 확인함.
3. 그래서 최종 구조: **구글 비전 API(Web Detection)가 후보 URL을 찾고 → Gemini가 `url_context`로 그 URL들을 직접 열어서 진짜 맞는지 확인**. 둘 다 무료 한도 안에서 동작.

**중요**: 이 구조에서 구글 비전 API 키는 사실상 필수입니다 — 없으면 Gemini가 열어볼 후보 URL이 없어서, 자기가 훈련 데이터로 "기억"하는 것만으로 답하게 됩니다(유명한 사례는 맞히지만, 개인적으로 찍은 사진 등은 신뢰할 수 없음). 아래 "구글 비전 API 설정"을 꼭 같이 해주세요.

## 구조
- `index.html` — 무드보드 빌더 전체(레이아웃 엔진, PPT 내보내기, 출처 리포트). 순수 정적 파일, 빌드 단계 없음.
- `api/verify.py` — Vercel Python 서버리스 함수. `POST /api/verify`로 이미지(dataURI)를 받아 구글 비전 API로 후보 URL을 찾고, Gemini의 `url_context` 도구로 그 URL들을 실제로 열어서 확인한 결과 JSON을 돌려줌. 자세한 배경은 파일 맨 위 docstring 참고.
- `api/requirements.txt` — 서버 함수용 파이썬 의존성(`google-genai` SDK만, 구글 비전 호출은 표준 라이브러리 `urllib`만 씀).
- `vercel.json` — 정적 파일(`index.html`)과 파이썬 함수(`api/verify.py`)를 명시적으로 분리해서 빌드하는 설정(`builds`/`routes`). *주의: 여기에 `pyproject.toml`을 다시 추가하거나 `functions` 키로 바꾸지 말 것 — 둘 다 Vercel이 사이트 전체를 파이썬 앱으로 오인해서 정적 파일 라우팅이 깨지는 걸 실제로 겪었음(2026-09-01).*

이미지·검증 결과는 전부 **브라우저 localStorage**에만 저장됩니다. 여러 명이 같은 링크를 열어도 서로의 무드보드나 검증 결과는 절대 섞이지 않습니다(각자 자기 브라우저 것만 봄).

## 회사 와이파이에서 API가 막히는 문제
API 키는 Vercel 서버 환경변수로만 존재하고, 브라우저는 그 키를 절대 보지 않습니다. 브라우저 → 우리 서버는 일반 웹사이트 방문과 동일한 트래픽이라 회사 방화벽에 걸릴 일이 거의 없고, 서버 → Google API 호출은 사용자 네트워크를 아예 거치지 않습니다(Vercel 클라우드에서 나가는 요청). 즉 한 번 배포해두면 계속 켜져 있을 필요 없이 항상 이 상태로 동작합니다.

## 배포 방법 (Vercel, CLI 설치 없이)

이 컴퓨터에 Node.js/npm/Vercel CLI가 설치되어 있지 않아서, GitHub 저장소를 거쳐 Vercel 대시보드에서 연결하는 방식으로 안내합니다(전부 클릭 몇 번).

1. **Gemini API 키 발급 (무료)**: [aistudio.google.com](https://aistudio.google.com) → 구글 계정으로 로그인 → "Get API key" → 새 키 생성. 결제 정보 등록 없이 바로 발급됩니다.
2. **구글 비전 API 키 발급 (아래 "구글 비전 API 설정" 참고, 사실상 필수)**
3. GitHub 저장소에 최신 파일 업로드(이미 있는 저장소면 이 단계 생략, 파일만 덮어쓰기)
4. **Vercel 연결/환경변수**: Settings → Environments → Production → `GOOGLE_API_KEY`(Gemini용)와 `GOOGLE_VISION_SERVICE_ACCOUNT_JSON`(비전용, 서비스 계정 JSON 전체) 둘 다 추가 → Redeploy.

이후 코드를 수정하고 싶으면 저한테 다시 요청하시면 되고, 제가 같은 저장소에 push하면(또는 파일을 보내드리면 직접 업로드) Vercel이 재배포합니다.

## 구글 비전 API 설정 (사실상 필수)
텍스트 검색 없이는 로고·간판 같은 단서가 없는 사진을 못 찾기 때문에, 구글 Cloud Vision의 Web Detection(실제 픽셀 기반 역이미지검색)이 이 파이프라인의 유일한 "검색" 수단입니다.

**Gemini API 키와 이 구글 비전 키는 서로 다른 발급 절차예요** — 둘 다 구글 계정으로 하지만, Gemini는 AI Studio에서 원클릭으로 발급되고, 비전 API는 정식 Google Cloud 프로젝트를 만들고 그 안에서 API를 켠 다음 별도로 인증을 설정해야 합니다.

**단순 API 키로는 안 됩니다** — 실제로 시도해보니 Vision API의 `images:annotate` 엔드포인트가 API 키 인증 자체를 거부합니다(401 "API keys are not supported by this API", 2026-09-02 확인). **서비스 계정(Service Account)**을 만들어서 그 인증서(JSON 키 파일)를 써야 합니다.

1. [console.cloud.google.com](https://console.cloud.google.com) → 새 프로젝트 → "Vision API" 검색해서 Cloud Vision API "사용(Enable)"
2. 왼쪽 메뉴 "IAM 및 관리자" → "서비스 계정" → **"+ 서비스 계정 만들기"** → 이름은 아무거나(예: `vision-caller`) → 만들기
3. 역할(권한) 선택 화면에서 **"Cloud Vision API 사용자"**(또는 검색해서 안 보이면 "편집자/Editor") 역할 부여 → 완료
4. 방금 만든 서비스 계정 클릭 → **"키(Keys)"** 탭 → "키 추가" → "새 키 만들기" → **JSON** 선택 → 다운로드(`무슨이름-xxxxx.json` 파일이 컴퓨터에 저장됨)
5. 그 JSON 파일을 텍스트 에디터로 열어서 **내용 전체를 복사**
6. Vercel 환경변수에 `GOOGLE_VISION_SERVICE_ACCOUNT_JSON`이라는 이름으로 추가하고, Value에 방금 복사한 JSON 전체를 그대로 붙여넣기 (한 줄이든 여러 줄이든 상관없음 — JSON 형식 그대로만 붙여넣으면 됨)

이 JSON 키 파일은 API 키보다 훨씬 강력한 인증서라 취급에 더 주의가 필요해요 — 저한테도 절대 보여주지 마시고, Vercel 환경변수에만 붙여넣어 주세요. 월 1,000건까지 무료, 이후 1,000건당 $3.50. 이 환경변수가 없으면 Gemini가 후보 URL 없이 자기 지식만으로 답합니다(위 경고 참고).

## 비용
- **Gemini (`gemini-3.6-flash`) + `url_context` 도구**: 지금까지 테스트에서 과금 없이 동작 확인함(정확한 무료 한도는 문서화가 안 돼 있어서, 실제 사용하며 지켜봐야 함 — 검증 화면에 요청별 토큰 사용량이 표시됩니다).
- **구글 비전 API**: 월 1,000건까지 무료.
- 둘 다 결제 정보(카드) 자체를 등록하지 않은 상태라, 무료 한도를 넘어서면 조용히 과금되는 게 아니라 에러가 납니다 — 그럴 땐 저한테 알려주세요.
