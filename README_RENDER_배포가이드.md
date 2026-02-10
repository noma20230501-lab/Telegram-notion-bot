# 📦 Render 배포 가이드

## 1️⃣ GitHub 레포지토리 생성 (새로 만들기)

1. GitHub 접속: https://github.com/new
2. **Repository name**: `telegram-notion-bot` (원하는 이름)
3. **Private** 선택 (보안상 중요!)
4. **README 추가 안 함** (이미 파일들 있으니까)
5. Create repository

---

## 2️⃣ 현재 프로젝트를 GitHub에 업로드

### 터미널에서 실행:

```bash
# Git 초기화
git init

# 모든 파일 추가 (.env는 자동으로 제외됨)
git add .

# 첫 커밋
git commit -m "텔레그램 노션 봇 초기 버전"

# GitHub 레포지토리 연결 (아래 URL을 본인 것으로 변경!)
git remote add origin https://github.com/본인아이디/telegram-notion-bot.git

# 업로드
git branch -M main
git push -u origin main
```

---

## 3️⃣ Render에서 배포

### Render 대시보드에서:

1. **Render 접속**: https://dashboard.render.com/
2. **New +** 버튼 클릭
3. **Background Worker** 선택 ⚠️ (Web Service 아님!)

### 설정:

| 항목 | 값 |
|------|-----|
| **Name** | telegram-notion-bot |
| **Repository** | 방금 만든 GitHub 레포 선택 |
| **Branch** | main |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `python telegram_notion_bot.py` |

4. **Environment Variables** (환경변수) 추가:
   - `TELEGRAM_BOT_TOKEN`: 본인의 텔레그램 봇 토큰
   - `NOTION_TOKEN`: 본인의 노션 API 토큰
   - `NOTION_DATABASE_ID`: 본인의 노션 DB ID

5. **Free** 플랜 선택

6. **Create Background Worker** 클릭!

---

## 4️⃣ 배포 확인

- Render가 자동으로 빌드 & 배포 시작
- 로그에서 "🤖 봇이 시작되었습니다..." 메시지 확인
- 텔레그램에서 봇에게 메시지 보내서 테스트!

---

## 🔄 수정 후 재배포 방법

파일 수정 후:

```bash
git add .
git commit -m "수정 내용 설명"
git push
```

→ Render가 **자동으로 재배포**됩니다!

---

## 💡 주의사항

1. **.env 파일은 GitHub에 올라가지 않습니다** (.gitignore에 설정됨)
2. **환경변수는 Render 웹에서 직접 설정**해야 합니다
3. **무료 플랜 제한**:
   - 월 750시간 (한 달 = 720시간이니 충분)
   - 15분 비활성화 시 sleep (텔레그램 봇은 영향 없음)

---

## 🆘 문제 해결

### 봇이 응답 안 하면?
1. Render 대시보드 → Logs 확인
2. 환경변수 제대로 입력했는지 확인
3. "🤖 봇이 시작되었습니다" 메시지 있는지 확인

### 배포 실패하면?
1. `requirements.txt` 파일 있는지 확인
2. Python 버전 문제 시 `runtime.txt` 추가:
   ```
   python-3.11.0
   ```
