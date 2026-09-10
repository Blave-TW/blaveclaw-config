"""機器側事件通道(config 側的 dual-write 入口)。

規格與分級在 `.claude/docs/notifications.md`;檔案格式、id 契約、輪替與 ack 的實作
在 runtime 的 `events.py`(機器上是 `<BASE>/current/events.py`)。**這裡刻意不重寫一份
append**:id 必須單調遞增、而且要比檔案現有最後一筆大(時鐘被 NTP 往回撥時新事件才不會
掉到平台的 ack 水位線底下、永遠送不出去)——那個契約只該有一份實作,兩份遲早會走鐘。

為什麼事件要落檔而不是機器自己送:機器自己判斷、自己發告警要送得出去,得同時滿足
「機器活著＋排程還在＋網路通＋Telegram 有配對」——那正是出事時最不可能同時成立的四件事,
而現役機隊將近一半根本沒配對 Telegram(那些通知今天靜默降級成一行 log)。事件先落檔,
由 reporter 每 2 分鐘搭既有回報送上平台,平台判斷級別、決定送哪些管道。

**只 append,永遠不改檔。** 輪替與截斷只有 reporter 做——它是唯一知道 ack 水位線的人。
兩邊都改檔的話,append 與重寫之間的競態會直接吃掉事件。

**兩個型別這裡絕對不要寫**:`halt` 與 `order_error`。它們已經由平台 diff 回報 payload
(`halt` 欄位、`order_errors` 陣列)產生,機器再寫一份就是同一件事落兩筆、P1 送兩則。

**冷卻交給平台,不要在這裡壓。** 呼叫端現有的 24h/6h stamp 是給 Telegram 那一半用的
(dual-write 期間照舊);事件本身每次都送,由平台依級別去重(P2 同因 6 小時)。這正是
「機器只回報事實,平台判斷」——機器自己壓過一輪,平台就永遠看不到那些被壓掉的事實。
"""
import logging
import os

_appender = None
_unavailable_logged = False


def _resolve():
    """runtime 的 events.append。找不到就 None(舊機沒有這支模組)。

    路徑跟 lib/notify 的 BLAVECLAW_HOME 解析同源:runtime 的 payload 解在
    `<BASE>/current/`,而 `<BASE>` 在 Blave Agent 機是 /opt/blave-agent、
    Windows 是 C:\\blave-agent。"""
    global _appender
    if _appender is not None:
        return _appender
    base = os.environ.get("BLAVE_AGENT_BASE")
    if not base:
        base = r"C:\blave-agent" if os.name == "nt" else "/opt/blave-agent"
    path = os.path.join(base, "current", "events.py")
    if not os.path.isfile(path):
        return None
    # 用檔案路徑載入,不是 `import events`:後者是裸模組名,workspace 或任何策略
    # 目錄只要有自己的 events.py / events/ 就會被先撈到,而且是安靜地撈錯——
    # 事件會寫到一個不存在的通道、誰都不會發現。
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("blave_runtime_events", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _appender = mod.append
        return _appender
    except Exception as e:
        logging.warning(f"[events] runtime events module unusable ({e})")
        return None


def emit(ev_type, **payload):
    """事件落檔。Best-effort:**永不 raise**——通知不能反過來炸掉下單或策略。

    舊機(runtime 沒有 events.py)一律 no-op,而且只記一次 log:dual-write 期間
    `lib/notify` 那一半照送,不會漏掉通知。

    payload 只送有值的欄位;平台端一律過白名單、截長度,多送的會被丟掉。"""
    global _unavailable_logged
    try:
        append = _resolve()
        if append is None:
            if not _unavailable_logged:
                _unavailable_logged = True
                logging.info("[events] runtime events channel unavailable — "
                             "telegram-only on this machine")
            return None
        return append(ev_type, {k: v for k, v in payload.items() if v is not None})
    except Exception as e:
        logging.warning(f"[events] emit {ev_type} failed: {e}")
        return None
