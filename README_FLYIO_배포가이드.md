# 🚀 Fly.io 무료 배포 가이드

**완전 무료**로 텔레그램 봇을 24시간 운영할 수 있습니다!

---

## 📋 준비사항

1. **Fly.io 계정** (무료)
2. **신용카드** (등록만, 과금 안 됨)
3. **본인의 토큰들** (텔레그램, 노션)

---

## 1️⃣ Fly.io CLI 설치

### Windows (PowerShell에서 실행):

```powershell
powershell -Command "iwr https://fly.io/install.ps1 -useb | iex"
```

설치 후 **터미널을 재시작**하세요!

---

## 2️⃣ Fly.io 로그인

```bash
fly auth login
```

브라우저가 열리면 로그인하세요.

---

## 3️⃣ 앱 생성 및 배포

### 3-1. 프로젝트 폴더로 이동

```bash
cd C:\Users\Administrator\Desktop\telegram_notion_bot
```

### 3-2. Fly.io 앱 생성

```bash
fly launch
```

질문이 나오면 아래와 같이 답하세요:

| 질문 | 답변 |
|------|------|
| **App Name?** | 엔터 (자동 생성) 또는 원하는 이름 입력 |
| **Choose a region** | Tokyo (nrt) 선택 |
| **Would you like to set up a Postgresql database?** | **No** (n) |
| **Would you like to set up an Upstash Redis database?** | **No** (n) |
| **Would you like to deploy now?** | **No** (n) ← 환경변수 먼저 설정해야 함 |

---

## 4️⃣ 환경변수 설정 (중요!)

**본인의 실제 토큰으로 교체**하세요:

```bash
fly secrets set TELEGRAM_BOT_TOKEN="본인의_텔레그램_봇_토큰"
fly secrets set NOTION_TOKEN="본인의_노션_API_토큰"
fly secrets set NOTION_DATABASE_ID="본인의_노션_DB_ID"
```

**예시:**
```bash
fly secrets set TELEGRAM_BOT_TOKEN="1234567890:ABCdefGHIjklMNOpqrsTUVwxyz"
fly secrets set NOTION_TOKEN="secret_ABC123XYZ456..."
fly secrets set NOTION_DATABASE_ID="a1b2c3d4e5f6..."
```

---

## 5️⃣ 배포!

```bash
fly deploy
```

배포가 시작됩니다! (1-2분 소요)

---

## 6️⃣ 확인

### 로그 확인:
```bash
fly logs
```

`🤖 봇이 시작되었습니다...` 메시지가 보이면 성공!

### 상태 확인:
```bash
fly status
```

### 텔레그램에서 테스트:
봇에게 메시지를 보내서 노션에 등록되는지 확인!

---

## 🔄 코드 수정 후 재배포

파일 수정 후:

```bash
# GitHub에 커밋 (선택사항)
git add .
git commit -m "수정 내용"
git push

# Fly.io 재배포
fly deploy
```

---

## 💰 비용 걱정 없는 이유

**Fly.io Free Tier:**
- 무료 VM: 3개까지
- 무료 메모리: 256MB x 3 = 768MB
- 무료 CPU: shared-cpu-1x x 3

**텔레그램 봇 사용량:**
- 메모리: ~50MB
- CPU: 거의 안 씀 (메시지 올 때만 작동)

→ **완전 무료로 평생 사용 가능!** ✅

---

## 🛠 유용한 명령어

```bash
# 앱 시작
fly apps restart

# 앱 정지
fly scale count 0

# 앱 재시작
fly scale count 1

# 환경변수 확인
fly secrets list

# 대시보드 열기
fly dashboard
```

---

## 🆘 문제 해결

### 봇이 응답 안 하면?
```bash
fly logs
```
로그에서 오류 확인

### 환경변수 잘못 입력했으면?
```bash
fly secrets set TELEGRAM_BOT_TOKEN="올바른_토큰"
```
다시 설정 후 자동 재시작

### 배포 실패하면?
```bash
fly deploy --verbose
```
상세 로그 확인

---

## 📞 추가 정보

- **Fly.io 대시보드**: https://fly.io/dashboard
- **Free Tier 상세**: https://fly.io/docs/about/pricing/
- **문서**: https://fly.io/docs/

---

## 🎉 완료!

이제 PC를 끄고 외출해도 봇이 24시간 작동합니다!
