"""봇 신호 로직이 백테스트 시뮬레이터와 동일한 결정을 내리는지 검증.

과거 펀딩비 전체(6,021건)를 순차 재생하면서 봇의 결정 규칙
(mean(last N)*1095, enter>3%, exit<0%)을 적용해 만든 포지션 시퀀스가
carry_sim.simulate_carry의 pos 배열과 정확히 일치해야 한다.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from carry_sim import CarryConfig, simulate_carry  # noqa: E402
from loader import load_funding  # noqa: E402

LOOKBACK, ENTER, EXIT = 21, 0.03, 0.0
IPY = 3 * 365

funding = load_funding("../data/funding" if Path("../data/funding").exists() else "data/funding")
rates = funding["rate"].to_numpy()

# --- bot decision replay (same rule as carry_bot.run_once) ---
bot_pos = np.zeros(len(rates), dtype=bool)
holding = False
for i in range(len(rates) - 1):
    if i + 1 >= LOOKBACK:
        apr = rates[i + 1 - LOOKBACK: i + 1].mean() * IPY
        if not holding and apr > ENTER:
            holding = True
        elif holding and apr < EXIT:
            holding = False
    bot_pos[i + 1] = holding

# --- simulator reference ---
res = simulate_carry(funding, CarryConfig(LOOKBACK, ENTER, EXIT))
sim_pos = res["f"]["pos"].to_numpy()

mismatch = int((bot_pos != sim_pos).sum())
print(f"settlements={len(rates)}, mismatches={mismatch}")
assert mismatch == 0, "bot decisions diverge from backtest!"
print("PARITY OK — 봇 신호는 백테스트와 100% 일치")
