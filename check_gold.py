# -*- coding: utf-8 -*-
"""
金价提醒 · 云端检查器（GitHub Actions 专用）

在 GitHub Actions 的 Runner 上运行：拉取金价 → 与 thresholds.json 里的提醒线比较 →
跨线时通过 Server酱推送微信。跨线标记写入 state.json，由 Actions 缓存跨运行保留，
保证「同一方向只推一次，回到区间内自动复位」。

环境变量：
  SCT_KEY    Server酱 SendKey（在仓库 Secrets 里配置）
  TEST_PUSH  设为 1 时额外发一条测试推送，用于验证通道
  DRY_RUN    设为 1 时不真发推送，只在日志里打印（本地调试用）

本地调试：
  python check_gold.py          # 真检查、跨线才推
  DRY_RUN=1 python check_gold.py
"""
import csv
import io
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, "thresholds.json")
STATE_PATH = os.path.join(BASE, "state.json")

CST = timezone(timedelta(hours=8))          # 北京时间
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "*/*",
}
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

DRY_RUN = os.environ.get("DRY_RUN", "") not in ("", "0", "false")
TEST_PUSH = os.environ.get("TEST_PUSH", "") not in ("", "0", "false")
SCT_KEY = (os.environ.get("SCT_KEY") or "").strip()


def log(msg):
    print("[%s] %s" % (datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def http(url, data=None, headers=None, timeout=25):
    h = dict(UA)
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return r.status, r.read().decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# 行情源（多源兜底，任一可用即可）
# --------------------------------------------------------------------------- #
def src_eastmoney():
    """东方财富批量接口：一次拿全 国际金价 / 国内金价 / 汇率"""
    qs = ("secids=122.XAU,118.AU9999,133.USDCNH"
          "&fields=f2,f3,f4,f12,f14,f15,f16,f124&fltt=2&invt=2")
    out = {}
    for host in ("push2delay.eastmoney.com", "push2.eastmoney.com"):
        try:
            st, body = http("https://%s/api/qt/ulist.np/get?%s" % (host, qs))
            diff = (json.loads(body).get("data") or {}).get("diff") or []
            for it in diff:
                code = it.get("f12")
                try:
                    price = float(it["f2"])
                except Exception:
                    continue        # 该品种停牌/无报价（值为 "-"），跳过不影响其它品种
                out[code] = {
                    "price": price, "pct": float(it.get("f3") or 0),
                    "chg": float(it.get("f4") or 0),
                    "high": float(it.get("f15") or 0), "low": float(it.get("f16") or 0),
                    "ts": int(it.get("f124") or 0), "name": it.get("f14"),
                }
            if out:
                return out, "eastmoney"
        except Exception as e:
            log("eastmoney(%s) 失败: %r" % (host, e))
    return {}, ""


def src_sina():
    """新浪：hf_XAU 伦敦金、gds_AU9999 上金所黄金9999（需 Referer）"""
    out = {}
    try:
        st, body = http("https://hq.sinajs.cn/list=hf_XAU,gds_AU9999",
                        headers={"Referer": "https://finance.sina.com.cn"})
        for line in body.split("\n"):
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            fields = val.strip().strip('";').split(",")
            try:
                if "hf_XAU" in key and len(fields) >= 3 and fields[0]:
                    out["XAU"] = {"price": float(fields[0]), "pct": None, "chg": None,
                                  "high": float(fields[4]) if len(fields) > 4 and fields[4] else 0,
                                  "low": float(fields[5]) if len(fields) > 5 and fields[5] else 0,
                                  "ts": int(time.time()), "name": "伦敦金"}
                elif "gds_AU9999" in key and len(fields) >= 2 and fields[1]:
                    out["AU9999"] = {"price": float(fields[1]), "pct": None, "chg": None,
                                     "high": 0, "low": 0,
                                     "ts": int(fields[-1]) if fields[-1].isdigit() else int(time.time()),
                                     "name": "黄金9999"}
            except Exception:
                continue        # 单条解析失败不影响其它品种
    except Exception as e:
        log("sina 失败: %r" % (e,))
    return out, ("sina" if out else "")


def src_stooq():
    """stooq：全球可访问，作为国际金价的最后兜底（只有国际金价）"""
    try:
        st, body = http("https://stooq.com/q/l/?s=xauusd&f=sd2t2ohlcv&h&e=csv")
        rows = list(csv.DictReader(io.StringIO(body)))
        if rows and rows[0].get("Close"):
            return {"XAU": {"price": float(rows[0]["Close"]), "pct": None, "chg": None,
                            "high": float(rows[0].get("High") or 0),
                            "low": float(rows[0].get("Low") or 0),
                            "ts": int(time.time()), "name": "XAU/USD"}}, "stooq"
    except Exception as e:
        log("stooq 失败: %r" % (e,))
    return {}, ""


def fetch_quotes():
    quotes, used = {}, []
    for fn in (src_eastmoney, src_sina):
        if quotes.get("XAU") and quotes.get("AU9999"):
            break
        q, name = fn()
        for k, v in q.items():
            quotes.setdefault(k, v)
        if name:
            used.append(name)
    if not quotes.get("XAU"):
        q, name = src_stooq()
        quotes.update(q)
        if name:
            used.append(name)
    return quotes, "+".join(used)


# --------------------------------------------------------------------------- #
# 推送
# --------------------------------------------------------------------------- #
def push_wechat(title, desp):
    if DRY_RUN:
        log("[DRY_RUN] 不实际发送")
        log("  TITLE: %s" % title)
        log("  DESP:\n%s" % desp)
        return True, "DRY_RUN 已跳过"
    if not SCT_KEY:
        return False, "未配置 SCT_KEY"
    url = "https://sctapi.ftqq.com/%s.send" % SCT_KEY
    data = urllib.parse.urlencode({"title": title, "desp": desp}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, headers=UA)
        with urllib.request.urlopen(req, timeout=25, context=SSL_CTX) as r:
            body = r.read().decode("utf-8", "replace")
        try:
            j = json.loads(body)
        except Exception:
            return True, "HTTP %s" % r.status
        if j.get("code") == 0:
            return True, "已推送"
        return False, "推送失败: %s %s" % (j.get("code"), j.get("message") or body[:120])
    except urllib.error.HTTPError as e:
        return False, "推送 HTTP %s %s" % (e.code, e.read().decode("utf-8", "replace")[:120])
    except Exception as e:
        return False, "推送异常: %r" % (e,)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def fmt(v, d=2):
    try:
        return ("%." + str(d) + "f") % float(v)
    except Exception:
        return "—"


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def main():
    cfg = load_json(CFG_PATH, None)
    if not cfg:
        log("ERROR thresholds.json 读不到")
        return 2

    gap = float(cfg.get("resetGapPct", 0.3)) / 100.0
    page = cfg.get("pageUrl", "")
    state = load_json(STATE_PATH, {}) or {}
    flags = state.get("flags") or {}

    quotes, src = fetch_quotes()
    if not quotes:
        log("ERROR 所有行情源都失败")
        return 1
    log("行情来源: %s | %s" % (src, {k: v["price"] for k, v in quotes.items()}))

    meta = {t.get("code"): t for t in cfg.get("targets") or []}

    def summary_line():
        # 双品种行情摘要，恒放推送正文第一行（国内在前）
        segs = []
        for c in ("AU9999", "XAU"):
            v, m = quotes.get(c), meta.get(c)
            if v and v.get("price") and m:
                segs.append("%s %s %s" % (m.get("name"), fmt(v["price"]), m.get("unit", "")))
        return "当前：" + "｜".join(segs)

    now_ts = int(time.time())
    parts, pushed = [], []

    for t in cfg.get("targets") or []:
        code, name = t.get("code"), t.get("name") or t.get("code")
        unit = t.get("unit") or ""
        q = quotes.get(code)
        if not q:
            parts.append("%s 无行情" % name)
            continue
        price = q["price"]
        stale = q.get("ts") and (now_ts - q["ts"]) > 5400
        f = flags.get(code) or {"above": False, "below": False}
        events = []

        above, below = t.get("above"), t.get("below")
        if above is not None and price >= float(above):
            if not f.get("above"):
                f["above"] = True
                if not stale:
                    events.append(("涨破", float(above)))
        elif above is not None and price <= float(above) * (1 - gap):
            f["above"] = False

        if below is not None and price <= float(below):
            if not f.get("below"):
                f["below"] = True
                if not stale:
                    events.append(("跌破", float(below)))
        elif below is not None and price >= float(below) * (1 + gap):
            f["below"] = False

        flags[code] = f

        for kind, line in events:
            cn_q = quotes.get("AU9999") or {}
            cn_price = fmt(cn_q["price"]) if cn_q.get("price") else None
            if code == "AU9999":
                title = "金价提醒·国内金价 %s 已%s %s" % (fmt(price), kind, fmt(line))
            elif cn_price:
                # 飞哥主看国内：国际触发时标题也带国内现价；Server酱标题上限32字，此格式最长约30
                title = "金价提醒·国内金价 %s｜国际已%s %s" % (cn_price, kind, fmt(line))
            else:
                title = "金价提醒·%s %s 已%s %s" % (name, fmt(price), kind, fmt(line))
            desp = "\n".join([
                summary_line(),
                "",
                "**%s** 当前 **%s %s**" % (name, fmt(price), unit),
                "",
                "- 触发条件：%s %s %s" % (kind, fmt(line), unit),
                "- 涨跌：%s (%s%%)" % (fmt(q.get("chg")), fmt(q.get("pct"))),
                "- 今日区间：%s ~ %s" % (fmt(q.get("low")), fmt(q.get("high"))),
                "- 行情来源：%s" % src,
                "- 检查时间：%s（北京时间）" % datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
                "- 说明：本提醒由 GitHub Actions 云端检查发出，电脑关机也有效",
                "- 实时看板：%s" % page,
            ])
            ok, msg = push_wechat(title, desp)
            log("PUSH %s | %s | %s" % ("OK" if ok else "FAIL", title, msg))
            if ok:
                pushed.append("%s %s → %s" % (name, kind, fmt(line)))

        tag = " (行情滞后，未推送)" if stale else ""
        parts.append("%s %s%s" % (name, fmt(price), tag))

    state["flags"] = flags
    state["lastCheck"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    state["lastQuotes"] = {k: {"price": v["price"], "ts": v.get("ts")} for k, v in quotes.items()}
    state["source"] = src
    save_json(STATE_PATH, state)

    summary = " | ".join(parts) if parts else "无数据"
    if pushed:
        summary += " || 已推送: " + "; ".join(pushed)
    else:
        summary += " || 未跨线，未推送"

    if TEST_PUSH:
        ok, msg = push_wechat("金价提醒 · 云端通道测试",
                              "如果你在微信里看到这条，说明 GitHub Actions 云端检查 + 推送通道已经打通。\n\n"
                              "本次行情：" + summary)
        log("TEST PUSH %s | %s" % ("OK" if ok else "FAIL", msg))
        summary += " || 测试推送: " + msg

    log("SUMMARY " + summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
