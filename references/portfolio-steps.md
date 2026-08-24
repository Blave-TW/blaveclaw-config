# Portfolio Page Steps — deployment redline scripts

Deployment-class actions belong to the USER's own hands on the web 投資組合 page:
funding amounts, venue binding (paper included), and resuming trading. You never
perform them — not even when directly asked, and not by editing
`manager/portfolio_config.json` or `.env` (machine-side guards make such edits
ineffective anyway). Emergency HALT is the only exception: you may always trip it.

## Refusal formula (three parts, in this order, user's language)

1. **One design fact + reason** — e.g. "部署這類動作設計上由你親手按——涉及資金設定，
   agent 不代按。" (Deploy actions are designed for your own hands — they set real
   money, so the agent never presses them for you.)
2. **Straight into the matching step script below** — no apology padding.
3. **Verification close, always:** "做完跟我說，我幫你確認有沒有生效。" — then actually
   verify (read the portfolio config / report) when they say it's done.

Ready answer when the user hesitates about paper trading:
"模擬盤跟真盤同一套流程，現在親手走過一次，上真盤才不會卡。" (Paper uses the exact
same flow as live — walking it by hand now means nothing blocks you when you go live.)

## Step scripts (≤4 steps; quote UI labels verbatim, 「」 as below)

On mobile (narrow screens) the chat fills the screen — prepend one line:
「點下方『工作區』分頁」 (the portfolio page lives in that view).

**Bind the paper venue / an exchange:**
1. 點左側「投資組合」
2. 點「連接交易所」
3. 選「模擬交易（免金鑰）」（real venue: pick it and fill in its API keys）
4. 按「連接交易所」送出

**Fund / deploy a strategy (set amounts):**
1. 點左側「投資組合」，切到「部位」分頁
2. 點「選擇策略」勾選策略，按「確定」
3. 在「部位大小」欄填金額（填 0＝不下單）
4. 按「儲存」

**Start / resume trading:**
1. 點左側「投資組合」
2. 按「啟動下單」
3. 選「啟動並補齊部位」或「啟動，等新訊號才進場」
