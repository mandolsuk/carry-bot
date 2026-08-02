# carry_bot — 필터드 펀딩 캐리 (현물 롱 + 선물 숏)

백테스트(2021.01~2026.06, 실제 펀딩비 6,021건)로 검증된 전략의 실행 봇.
신호 로직은 백테스트 시뮬레이터와 100% 일치 검증됨 (`test_signal_parity.py`).

- 신호: 펀딩 7일 평균 연환산 > 3% 진입, < 0% 청산
- 실행: 8시간 펀딩 정산 직후 1회 실행(oneshot) — 파이 부하 거의 0
- 모드: `paper`(주문 없음) → `testnet`(선물 테스트 주문) → `live`(실주문)

## 1. 파이에서 기존 프로그램 종료

```bash
# 돌고 있는 파이썬 프로세스 확인
ps aux | grep -E "python|btc" | grep -v grep
# systemd 서비스로 돌고 있다면
systemctl list-units --type=service | grep -iE "bot|btc|trad"
sudo systemctl stop <서비스명> && sudo systemctl disable <서비스명>
# cron이라면
crontab -l    # 해당 줄 주석 처리: crontab -e
# 수동 실행 중이라면
pkill -f btc_trader.py   # 프로세스명에 맞게
```

## 2. 설치

```bash
cd ~ && unzip carry_bot.zip -d carry_bot && cd carry_bot
pip3 install ccxt pyyaml requests --break-system-packages
cp .env.example .env && nano .env      # 텔레그램 토큰 입력 (키는 3단계에서)
```

`config.yaml` 확인: `paper_use_testnet_data`는 개발용 플래그이므로
파이에서는 `false`로 두세요 (paper 모드가 실시세를 사용하게 됨).

## 3. 단계별 가동

**1단계 — paper (1~2주 권장)**: 키 불필요. `mode: paper` 상태로:

```bash
python3 carry_bot.py     # 수동 1회 실행 테스트
```

**2단계 — testnet**: https://testnet.binancefuture.com 가입(가짜 돈) → API 키 발급
→ `.env`에 `BINANCE_FUT_KEY/SECRET` 입력 → `config.yaml`에서 `mode: testnet`.
선물 숏 레그가 실제 테스트 주문으로 나감.

**3단계 — live**: 바이낸스 실계정에서 현물/선물 API 키 발급.
반드시 ① 출금 권한 OFF, ② 거래 권한만 ON, ③ 파이 공인 IP 화이트리스트.
`config.yaml`: `mode: live`, `capital_usdt`를 소액(예: 300)부터.

## 4. 자동 실행 등록 (systemd 타이머)

```bash
# carry-bot.service의 User/경로가 본인 환경과 맞는지 확인 후
sudo cp carry-bot.service carry-bot.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now carry-bot.timer
systemctl list-timers | grep carry     # 다음 실행 시각 확인
journalctl -u carry-bot.service -n 30  # 로그 확인
```

정산 시각(00/08/16 UTC = 한국 09/17/01시) 5분 후 실행됩니다.

## 5. 상태 확인

```bash
python3 - <<'EOF'
import sqlite3
conn = sqlite3.connect("carry_state.db")
for row in conn.execute("SELECT * FROM events ORDER BY ts DESC LIMIT 10"):
    print(row)
print("state:", dict(conn.execute("SELECT * FROM state")))
EOF
```

텔레그램: 진입/청산/오류 시 자동 알림. `heartbeat: true`로 바꾸면 매 실행마다 상태 전송.

## 리스크 메모 (live 전 필독)

- 실질 기대수익은 자본 대비 연 4~5% 수준 (검증 구간 기준). 화려하지 않은 대신 방향 무관.
- BTC 급등 시 선물 계좌 증거금 비율 확인 필요 — 현물 이익으로 상쇄되지만 계좌가 분리되어
  있으므로, 증거금 부족 알림(⚠️ 레그 불일치)이 오면 현물→선물 계좌로 USDT 이체.
- 펀딩이 마이너스로 길게 가면 봇이 자동 청산 후 현금 대기 (2022년 구간에서 작동 확인).
- 투자 자문 아님. 소액으로 시작할 것.
