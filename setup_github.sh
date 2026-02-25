#!/bin/bash
# ─────────────────────────────────────────────────────────────
# GitHub 최초 업로드 스크립트
# 사용법: Git Bash에서 ./setup_github.sh 실행
# ─────────────────────────────────────────────────────────────

echo ""
echo "=== ATR Monitor GitHub 업로드 ==="
echo ""

# ── GitHub repo URL 설정 ──────────────────────────────────────
# 인자로 넘긴 경우 우선 사용: bash setup_github.sh <URL>
if [ -n "$1" ]; then
  REPO_URL="$1"
else
  # 아래 줄에 직접 URL 입력 (붙여넣기가 안 될 때 사용)
  REPO_URL="https://github.com/Lukedino/ATR_Monitoring.git"
fi

echo "사용할 repo URL: $REPO_URL"
echo ""

# ── git 사용자 정보 설정 (커밋에 필요) ───────────────────────
git config user.name  "Lukedino"
git config user.email "lukedino@users.noreply.github.com"

# ── git 초기화 ────────────────────────────────────────────────
echo ""
echo "[1/5] git 초기화..."
git init

# ── 전체 파일 스테이징 ────────────────────────────────────────
echo "[2/5] 파일 스테이징..."
git add .

# ── 첫 커밋 ──────────────────────────────────────────────────
echo "[3/5] 첫 커밋 생성..."
git commit -m "feat: initial ATR trailing stop monitor"

# ── main 브랜치 설정 ──────────────────────────────────────────
echo "[4/5] main 브랜치 설정..."
git branch -M main

# ── remote 연결 & push ────────────────────────────────────────
echo "[5/5] GitHub 연결 및 업로드..."
# 이미 remote가 등록되어 있으면 제거 후 재등록
git remote remove origin 2>/dev/null
git remote add origin "$REPO_URL"
git push -u origin main

echo ""
echo "=== 완료! ==="
echo ""
echo "다음 단계: GitHub repo > Settings > Secrets and variables > Actions"
echo "아래 5개 Secret을 등록하세요:"
echo ""
echo "  TELEGRAM_BOT_TOKEN  : 텔레그램 봇 토큰"
echo "  TELEGRAM_CHAT_ID    : 텔레그램 채팅 ID"
echo "  ACCOUNT_SIZE        : 계좌 규모 (예: 10000000)"
echo "  RISK_PER_TRADE      : 리스크 비율 (예: 0.01)"
echo "  STOCK_LIST          : 종목 JSON (아래 형식)"
echo ""
echo '  {"국내주식":["005930.KS"],"해외주식":["AAPL","NVDA"],"암호화폐":["BTC-USD"],"ETF":["SPY"]}'
echo ""
