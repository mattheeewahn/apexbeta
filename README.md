# APEX – 외국인 친구 만들기 앱

Flask + Socket.IO 단일 서버입니다. 서버가 `index.html`(프론트)과 API를 같은 도메인에서 함께 제공합니다.

## 로컬 실행

```powershell
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   # 그리고 값 채우기 (로컬은 APP_ENV=development, REQUIRE_EMAIL_VERIFICATION=false 가능)
python server.py         # http://localhost:5000
```

## 배포 (Render / Railway / Fly.io 등)

1. 이 폴더를 GitHub 저장소로 올립니다. (`.env`, `instance/`, `venv/`는 `.gitignore`로 제외됨)
2. PostgreSQL 데이터베이스를 하나 만듭니다. (SQLite는 재배포 시 데이터가 사라지는 호스트가 많습니다)
3. 환경변수를 호스팅 대시보드에 설정합니다. 목록은 `.env.example` 참고. 필수:
   - `SECRET_KEY`, `DATABASE_URL`, `ADMIN_EMAILS`
   - `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` (이메일 인증용)
   - Google 로그인을 쓰면 `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`
     (Google Cloud Console에 `https://내도메인/auth/google/callback` 등록)
4. 시작 명령 (Procfile / Dockerfile에 이미 들어 있음):
   ```
   gunicorn -k gthread -w 1 --threads 100 --timeout 120 -b 0.0.0.0:$PORT server:app
   ```
   Socket.IO 때문에 워커는 반드시 1개(`-w 1`)로 유지하세요.
5. HTTPS는 호스트가 제공합니다. 직접 서버(라즈베리파이 등)에 올릴 경우 nginx/Caddy로 HTTPS를 붙이고 5000 포트를 외부에 직접 열지 마세요.

## 관리자

- 운영자 계정: `BOOTSTRAP_ADMIN_USERNAME` / `BOOTSTRAP_ADMIN_PASSWORD` (+ NAME, NATIONALITY, EMAIL)를 설정하면
  서버 첫 시작 때 admin 계정이 자동 생성됩니다. 이미 있으면 아무것도 하지 않습니다.
- `ADMIN_EMAILS`에 넣은 이메일로 가입해도 admin이 됩니다. 그 외에는 모두 일반 user입니다.
- admin은 프로필 사진에 금색 링, 이름 옆에 파란 체크 뱃지가 모든 사용자에게 표시됩니다.

## Google 로그인

처음 Google로 로그인하면 계정 설정 화면(아이디, 비밀번호, 국적, 생년월일)이 나오고,
완료해야 앱에 들어갈 수 있습니다. 완료 후에는 Google 로그인과 아이디/비밀번호 로그인 둘 다 됩니다.

## 기존 DB

기존 `instance/app.db`의 `school` 컬럼은 서버 시작 시 자동으로 `nationality`로 이름이 바뀝니다(데이터 유지).
