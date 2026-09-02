# 무드보드 빌더 (AI 출처 검증)

`bx-agent-workflow`의 아티팩트 버전과는 완전히 별개인 독립 웹앱입니다. 차이는 딱 하나: 이 앱은 서버(`api/verify.py`)가 있어서, "AI로 확인" 버튼을 누르면 그 서버가 Claude API(웹서치 도구 포함)를 실제로 호출해 이미지 출처를 그 자리에서 검증합니다. 아티팩트 버전은 이게 구조적으로 불가능해서(브라우저 안에서 외부 API 호출 자체가 막혀 있음) PDF 내보내기 같은 우회 방법을 썼는데, 여기서는 필요 없습니다.

## 구조
- `index.html` — 무드보드 빌더 전체(레이아웃 엔진, PPT 내보내기, 출처 리포트). 순수 정적 파일, 빌드 단계 없음.
- `api/verify.py` — Vercel Python 서버리스 함수. `POST /api/verify`로 이미지(dataURI)를 받아 (있으면) 구글 비전 API로 후보 출처를 먼저 찾고, Claude API(웹서치+웹페칭 도구 포함)로 실제로 맞는지 검증한 결과 JSON을 돌려줌.
- `api/requirements.txt` — 서버 함수용 파이썬 의존성(`anthropic` SDK만, 구글 비전 호출은 표준 라이브러리 `urllib`만 씀).
- `vercel.json` — 정적 파일(`index.html`)과 파이썬 함수(`api/verify.py`)를 명시적으로 분리해서 빌드하는 설정(`builds`/`routes`). *주의: 여기에 `pyproject.toml`을 다시 추가하거나 `functions` 키로 바꾸지 말 것 — 둘 다 Vercel이 사이트 전체를 파이썬 앱으로 오인해서 정적 파일 라우팅이 깨지는 걸 실제로 겪었음(2026-09-01).*

이미지·검증 결과는 전부 **브라우저 localStorage**에만 저장됩니다. 여러 명이 같은 링크를 열어도 서로의 무드보드나 검증 결과는 절대 섞이지 않습니다(각자 자기 브라우저 것만 봄).

## 회사 와이파이에서 API가 막히는 문제
API 키는 Vercel 서버 환경변수로만 존재하고, 브라우저는 그 키를 절대 보지 않습니다. 브라우저 → 우리 서버는 일반 웹사이트 방문과 동일한 트래픽이라 회사 방화벽에 걸릴 일이 거의 없고, 서버 → Anthropic API 호출은 사용자 네트워크를 아예 거치지 않습니다(Vercel 클라우드에서 나가는 요청). 즉 한 번 배포해두면 계속 켜져 있을 필요 없이 항상 이 상태로 동작합니다.

## 배포 방법 (Vercel, CLI 설치 없이)

이 컴퓨터에 Node.js/npm/Vercel CLI가 설치되어 있지 않아서, GitHub 저장소를 거쳐 Vercel 대시보드에서 연결하는 방식으로 안내합니다(전부 클릭 몇 번).

1. **Anthropic API 키 발급**: [console.anthropic.com](https://console.anthropic.com) → 계정 생성 → Billing에서 결제수단 등록(소액 충전) → API Keys에서 키 발급. 키는 `sk-ant-...`로 시작합니다.
2. **GitHub에 빈 저장소 하나 생성**: github.com → New repository → 이름은 아무거나(예: `moodboard-verify-app`) → **Public/Private 상관없음** → 아무 파일도 추가하지 말고(README 체크 해제) 생성만.
3. 저한테 그 저장소 URL을 알려주시면, 제가 지금 이 코드를 그 저장소로 push해드릴게요.
4. **Vercel 연결**: [vercel.com](https://vercel.com) → GitHub 계정으로 로그인 → "Add New... → Project" → 방금 만든 저장소 선택 → Import.
5. Import 화면에서 "Environment Variables"에 `ANTHROPIC_API_KEY` = (2번에서 받은 키) 추가 → Deploy.
6. 배포되면 Vercel이 `https://<프로젝트명>.vercel.app` 같은 URL을 줍니다. 그게 팀원들과 공유할 링크예요.

이후 코드를 수정하고 싶으면 저한테 다시 요청하시면 되고, 제가 같은 저장소에 push하면 Vercel이 자동으로 재배포합니다(GitHub 연동 시 기본 동작).

### (선택) 구글 비전 API로 정확도 올리기
텍스트 검색만으로는 로고·간판 같은 단서가 없는 사진을 못 찾을 때가 있어서, 구글 Cloud Vision의 Web Detection(실제 픽셀 기반 역이미지검색)을 먼저 돌려 후보 링크를 찾고 Claude가 그걸 확인하도록 붙일 수 있습니다.
1. [console.cloud.google.com](https://console.cloud.google.com) → 새 프로젝트 → "Vision API" 검색해서 Cloud Vision API "사용(Enable)"
2. API 및 서비스 → 사용자 인증 정보 → API 키 생성
3. Vercel 환경변수에 `GOOGLE_VISION_API_KEY` 추가

이 환경변수가 없으면 그냥 기존 방식(Claude 자체 웹서치)으로만 동작합니다 — 필수 아님.

## 비용
검증 1회 호출당 이미지 비전 토큰(수백~1500) + 웹서치·웹페칭 도구 사용료(검색 1회당 $0.01, 검색 결과 내용도 입력 토큰으로 과금) + Claude Opus 5의 기본 활성화된 thinking 출력 토큰이 듭니다. 정확한 금액은 이미지마다 달라서 [console.anthropic.com](https://console.anthropic.com)의 Usage 페이지에서 실제 요청별 비용을 확인하는 게 정확해요. 구글 비전을 켜면 이미지 1,000장까지는 무료, 그 이후 1,000장당 $3.50가 추가됩니다.
