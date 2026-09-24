# ISXBridge MT5 Expert Advisor

`ISXBridge.mq5` is a thin MT5 adapter for the Python ISX engine. It sends
completed H4, H1, and M15 broker candles to the loopback bridge and receives a
deterministic ISX decision. The EA never asks Python to submit an order.

## Run it safely

1. Start the Python server from the repository:

   ```powershell
   .venv\Scripts\python.exe -m jevloop serve --port 8765
   ```

2. In MT5, copy `ISXBridge.mq5` into `MQL5/Experts/ISXBridge/`, compile it,
   and attach it to the desired symbol.
3. Add `http://127.0.0.1:8765` to **Tools → Options → Expert Advisors → Allow
   WebRequest for listed URL**.
4. Leave `InpShadowMode=true` and `InpEnableLiveExecution=false` while
   validating the protocol. The Experts log will show every phase and veto.
5. Set `InpBrokerUtcOffsetHours` to the broker server's UTC offset so rates
   are normalized before they reach Python. The bridge only accepts completed
   candles; the EA requests history from shift `1`, never shift `0`.

The Python endpoint is:

```text
POST http://127.0.0.1:8765/api/mt5/isx/decision
GET  http://127.0.0.1:8765/api/mt5/status
```

Set `JEV_MT5_BRIDGE_TOKEN` in the Python process and `InpBridgeToken` in the
EA to require a matching `X-Jev-Bridge-Token` header. The server binds only to
loopback by default.

Live order submission is deliberately guarded by both EA inputs and local
checks for terminal trading permission, duplicate ISX positions, spread, stop
distance, volume bounds, and shorting permission. The EA tags orders with the
ISX setup ID and magic number. Break-even is 1R, profit lock is 2R, and the
default target is 4R.
