#!/usr/bin/env python3
"""
200슨피단 신호 데이터 수집기 (26.08.14 최종판)

- Yahoo Finance에서 ^GSPC(SPX 순수지수)와 TQQQ 일봉 15년치를 받는다
- 날짜로 조인 → SMA200 계산 → data.json 저장
- 상태 머신을 돌려 신호를 판정하고, 상태가 "바뀌었을 때만" 텔레그램 알림

판정은 전부 확정 종가 기준. 장중 가격은 쓰지 않는다.

환경변수(선택):
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID  둘 다 있어야 알림 발송
"""

import json, os, sys, time, urllib.request, urllib.parse
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data.json")
STATE = os.path.join(ROOT, "state.json")

# ── 26.08.14 최종판 파라미터 ────────────────────────────────────
BAND    = 0.03      # 상·하단 밴드 ±3%
TS_DROP = 0.10      # 트레일링 스탑 −10%
TS_REF  = "SPX"     # TS 추적 기준 (SPX | TQQQ)
TQ_W    = 67        # 진입 시 TQQQ 비중 %
SMA_P   = 200
RANGE   = "15y"

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def fetch(symbol):
    """야후에서 일봉 종가를 {날짜: 종가}로 반환. null은 제거."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?range={RANGE}&interval=1d")
    last_err = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                j = json.load(r)
            res = j["chart"]["result"][0]
            closes = res["indicators"]["quote"][0]["close"]
            stamps = res["timestamp"]
            out = {}
            for v, t in zip(closes, stamps):
                if v is None or v <= 0:
                    continue
                d = datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m%d")
                out[d] = round(float(v), 2)
            if len(out) < SMA_P + 50:
                raise RuntimeError(f"데이터가 너무 적음: {len(out)}행")
            return out
        except Exception as e:      # 야후는 가끔 일시적으로 실패한다
            last_err = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"{symbol} 수집 실패: {last_err}")


def build_rows():
    spx, tq = fetch("^GSPC"), fetch("TQQQ")
    dates = sorted(d for d in spx if d in tq)      # ★ 날짜로 조인
    S = [spx[d] for d in dates]

    rows = []
    run = 0.0
    for i, d in enumerate(dates):
        run += S[i]
        if i >= SMA_P:
            run -= S[i - SMA_P]
        sma = round(run / SMA_P, 2) if i >= SMA_P - 1 else None
        if sma is None:
            continue                                # SMA200 이전 구간은 버림
        rows.append([d, S[i], sma, tq[d]])
    return rows


def analyze(rows):
    """공식 위젯 analyzeSignal 구조 + 26.08.14 리밸런싱 규칙."""
    pos, peak, days, rebal = "BELOW", 0.0, 0, False
    entry = None, None
    cycle_max = 0.0
    events, alert, rebal_today = [], False, False

    for d, S, sma, T in rows:
        bU, bD = sma * (1 + BAND), sma * (1 - BAND)
        ref = S if TS_REF == "SPX" else T

        today = pos
        if S >= bU:
            today = "ABOVE"
        elif S < bD:
            today = "BELOW"
        zone = "위" if S >= bU else ("아래" if S < bD else "안")

        alert = rebal_today = False
        if pos == "BELOW" and today == "ABOVE":
            peak, days, rebal = ref, 1, False
            entry, cycle_max = (d, T), T
            events.append((d, "진입", T))
        elif pos == "ABOVE" and today == "BELOW":
            peak, days, rebal = 0.0, 0, False
            entry, cycle_max = (None, None), 0.0
            events.append((d, "전량탈출", T))
        elif today == "ABOVE":
            days += 1
            peak = max(peak, ref)
            cycle_max = max(cycle_max, T)
            if ref <= peak * (1 - TS_DROP):
                alert = True
                peak = ref                          # ★ 다회성: 고점 리셋
                events.append((d, "TS", T))
            if zone == "안" and not rebal:
                rebal = rebal_today = True
                events.append((d, "리밸런싱", T))
        pos = today

    d, S, sma, T = rows[-1]
    bU, bD = sma * (1 + BAND), sma * (1 - BAND)
    zone = "위" if S >= bU else ("아래" if S < bD else "안")
    ref_now = S if TS_REF == "SPX" else T
    return {
        "date": d, "spx": S, "sma": sma, "tqqq": T, "zone": zone,
        "upper": round(bU, 2), "lower": round(bD, 2),
        "position": pos, "daysAbove": days, "rebalanced": rebal,
        "rebalToday": rebal_today, "alert": alert,
        "peak": round(peak, 2), "cycleMax": round(cycle_max, 2),
        "tsTrigger": round(peak * (1 - TS_DROP), 2) if peak else None,
        "ddFromPeak": round((ref_now - peak) / peak * 100, 2) if peak else None,
        "toLower": round((bD / S - 1) * 100, 2),
        "entryDate": entry[0], "entryPx": entry[1],
        "events": [list(e) for e in events[-40:]],
    }


def headline(a):
    """오늘의 한 줄 신호."""
    tqw, spw = TQ_W, 100 - TQ_W
    if a["position"] == "BELOW":
        return "🔴 하단 밴드 이탈 — TQQQ·SPYM 전량 매도 후 SGOV 대피"
    if a["alert"]:
        return f"🚨 방어 브레이크 (SPX 고점 −{TS_DROP:.0%}) — TQQQ 50%를 SPYM으로"
    if a["rebalToday"]:
        return f"🔄 밴드 안 재진입 — TQQQ {tqw}% / SPYM {spw}%로 리밸런싱 (1회)"
    if a["daysAbove"] <= 3:
        return f"🟢 상단 밴드 돌파 {a['daysAbove']}일 차 — 목표의 {a['daysAbove']}/3 매수 (TQQQ {tqw} · SPYM {spw})"
    return f"⚪️ 추세 유지 {a['daysAbove']}일 차 — 오늘 할 일 없음"


def state_key(a):
    """이 값이 바뀌면 알림을 보낸다."""
    if a["position"] == "BELOW":
        return "BELOW"
    if a["alert"]:
        return f"TS@{a['date']}"
    if a["rebalToday"]:
        return f"REBAL@{a['date']}"
    if a["daysAbove"] <= 3:
        return f"ENTRY{a['daysAbove']}"
    return "HOLD"


def notify(text):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("텔레그램 설정 없음 — 알림 생략")
        return
    body = urllib.parse.urlencode({
        "chat_id": chat, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=body)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        print("텔레그램 발송 완료")
    except Exception as e:
        print(f"텔레그램 발송 실패: {e}", file=sys.stderr)


def main():
    rows = build_rows()
    a = analyze(rows)

    with open(DATA, "w", encoding="utf-8") as f:
        json.dump({
            "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "lastDate": a["date"],
            "params": {"band": BAND, "tsDrop": TS_DROP, "tsRef": TS_REF, "tqW": TQ_W},
            "signal": a,
            "rows": rows,
        }, f, separators=(",", ":"), ensure_ascii=False)

    print(f"{a['date']} | {a['position']} {a['daysAbove']}일 | "
          f"SPX {a['spx']:,} / 상단 {a['upper']:,} / 하단 {a['lower']:,} | "
          f"TS선 {a['tsTrigger']} | {len(rows)}행")
    print(headline(a))

    # 상태 변화가 있을 때만 알림
    prev = {}
    if os.path.exists(STATE):
        try:
            prev = json.load(open(STATE, encoding="utf-8"))
        except Exception:
            pass
    key = state_key(a)
    # "HOLD"(할 일 없음)로 돌아온 것은 알릴 일이 아니다.
    # 이걸 막지 않으면 TS·리밸런싱 다음 날마다 "추세 유지" 알림이 한 번 더 간다.
    if key == "HOLD":
        print("할 일 없음 — 알림 생략")
    elif prev.get("key") != key:
        dca = {"위": "SPYM 100%", "안": f"TQQQ {TQ_W}% · SPYM {100-TQ_W}%",
               "아래": "SGOV 100%"}[a["zone"]]
        msg = (f"<b>200슨피단 신호 변경</b>\n{a['date']} 종가 기준\n\n"
               f"{headline(a)}\n\n"
               f"SPX <b>{a['spx']:,}</b>  (200일선 {a['sma']:,})\n"
               f"상단 {a['upper']:,} / 하단 {a['lower']:,} ({a['toLower']:+.2f}%)\n")
        if a["tsTrigger"]:
            msg += (f"TS 발동선 <b>{a['tsTrigger']:,}</b> "
                    f"(고점 {a['peak']:,} 대비 현재 {a['ddFromPeak']:+.2f}%)\n")
        msg += f"TQQQ ${a['tqqq']:,}\n적립: {dca}"
        notify(msg)
    else:
        print(f"상태 변화 없음 ({key}) — 알림 생략")

    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({"key": key, "date": a["date"]}, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
