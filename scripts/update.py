#!/usr/bin/env python3
"""
Morgen-Briefing – Datenjob.

Holt Nachrichten (RSS), Marktdaten (Yahoo Finance, ohne Schlüssel) und wertet die
Artikel automatisch aus. Ist ein KI-Schlüssel (Secret AI_API_KEY) hinterlegt, schreibt
zusätzlich eine KI Zusammenfassungen, Kernpunkte, Einordnung und den Morgenbericht.

Nur Python-Standardbibliothek – keine Installation nötig.

Aufruf:  python3 scripts/update.py --out _site/data
Umgebungsvariablen (optional):
  PREV_URL      Adresse der bisher veröffentlichten App (für Zwischenspeicher)
  AI_API_KEY    Schlüssel für die KI (z. B. Google Gemini, kostenlos)
  AI_MODEL      überschreibt config.ai.model
  AI_BASE_URL   überschreibt config.ai.baseUrl
  EXTRA_FORCE_BRIEFING=1   Morgenbericht neu erzeugen, auch wenn heute schon vorhanden
"""
import argparse, datetime as dt, email.utils, hashlib, html, json, os, re, sys, time
import urllib.parse, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
NOW = dt.datetime.now(dt.timezone.utc)
LOG = []


def log(msg):
    LOG.append(msg)
    print(msg, flush=True)


def berlin_now():
    # Mitteleuropäische Zeit inkl. Sommerzeit (letzter Sonntag März/Oktober, 1:00 UTC)
    y = NOW.year
    def last_sunday(month):
        d = dt.datetime(y, month, 31 if month in (3, 10) else 30, 1, tzinfo=dt.timezone.utc)
        return d - dt.timedelta(days=(d.weekday() + 1) % 7)
    off = 2 if last_sunday(3) <= NOW < last_sunday(10) else 1
    return NOW + dt.timedelta(hours=off)


# ----------------------------------------------------------------------------- HTTP
def http_get(url, timeout=20, accept="*/*", tries=2):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept,
                                                       "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), r.geturl()
        except Exception as e:  # noqa
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def http_json(url, **kw):
    raw, _ = http_get(url, accept="application/json", **kw)
    return json.loads(raw.decode("utf-8", "replace"))


# ----------------------------------------------------------------------------- Text
class _Strip(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out = []

    def handle_data(self, d):
        self.out.append(d)


def strip_html(s):
    if not s:
        return ""
    p = _Strip()
    try:
        p.feed(html.unescape(s) if "&lt;" in s else s)
    except Exception:
        return re.sub(r"<[^>]+>", " ", s)
    t = " ".join(p.out)
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


class _Article(HTMLParser):
    """Sehr einfache Extraktion des Fließtexts (<p>-Absätze) einer Artikelseite."""
    SKIP = {"script", "style", "nav", "footer", "header", "aside", "form", "figure", "noscript"}

    def __init__(self):
        super().__init__()
        self.depth_skip = 0
        self.in_p = False
        self.cur = []
        self.paras = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.depth_skip += 1
        elif tag == "p" and not self.depth_skip:
            self.in_p, self.cur = True, []

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.depth_skip:
            self.depth_skip -= 1
        elif tag == "p" and self.in_p:
            t = re.sub(r"\s+", " ", "".join(self.cur)).strip()
            if len(t) > 60:
                self.paras.append(t)
            self.in_p = False

    def handle_data(self, d):
        if self.in_p and not self.depth_skip:
            self.cur.append(d)


def fetch_fulltext(url, limit=6000):
    """Nur als Eingabe für die KI – der Volltext wird NICHT veröffentlicht."""
    if not url or "news.google.com" in url:
        return ""
    try:
        raw, _ = http_get(url, timeout=12, accept="text/html", tries=1)
        p = _Article()
        p.feed(raw.decode("utf-8", "replace"))
        txt = "\n".join(p.paras)
        return txt[:limit]
    except Exception:
        return ""


def norm_tokens(title):
    t = re.sub(r"[^\wäöüß ]", " ", title.lower())
    stop = {"der", "die", "das", "und", "in", "im", "zu", "mit", "auf", "für", "von", "den", "dem", "des",
            "ein", "eine", "ist", "the", "a", "of", "to", "and", "for", "on", "as", "at", "is", "by", "an"}
    return {w for w in t.split() if len(w) > 2 and w not in stop}


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = email.utils.parsedate_to_datetime(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc)
    except Exception:
        pass
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc)
    except Exception:
        return None


# ----------------------------------------------------------------------------- Feeds
def feed_url(f):
    if f.get("url"):
        return f["url"]
    q = urllib.parse.quote_plus(f["q"] + " when:2d")
    if f.get("lang") == "en":
        return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    return f"https://news.google.com/rss/search?q={q}&hl=de&gl=DE&ceid=DE:de"


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _child(el, *names):
    for c in el:
        if _local(c.tag) in names:
            return c
    return None


def parse_feed(raw, f):
    root = ET.fromstring(raw)
    items = []
    entries = [e for e in root.iter() if _local(e.tag) in ("item", "entry")]
    for e in entries:
        title_el = _child(e, "title")
        title = strip_html(title_el.text if title_el is not None else "")
        link = ""
        l = _child(e, "link")
        if l is not None:
            link = (l.text or "").strip() or l.attrib.get("href", "")
        if not link:
            g = _child(e, "guid", "id")
            if g is not None and (g.text or "").startswith("http"):
                link = g.text.strip()
        desc_el = _child(e, "description", "summary", "content", "encoded")
        desc = strip_html(desc_el.text if desc_el is not None else "")
        date_el = _child(e, "pubDate", "published", "updated", "date")
        pub = parse_date(date_el.text if date_el is not None else None)
        src = f["name"]
        s_el = _child(e, "source")
        if s_el is not None and (s_el.text or "").strip():
            src = s_el.text.strip()
        if "news.google.com" in feed_url(f):
            # Google News: Titel "Schlagzeile - Quelle", Beschreibung ist nur ein Link
            if " - " in title:
                head, tail = title.rsplit(" - ", 1)
                if len(tail) < 60:
                    title, src = head.strip(), (src if src != f["name"] else tail.strip())
            if desc and norm_tokens(desc) <= norm_tokens(title) | norm_tokens(src):
                desc = ""
            if desc.startswith(title):
                desc = desc[len(title):].strip(" -–")
                if desc.strip() == src:
                    desc = ""
        if not title:
            continue
        if desc.strip() == title.strip():
            desc = ""
        items.append({
            "title": title[:300],
            "link": link,
            "source": src[:80],
            "published": (pub or NOW).isoformat(timespec="seconds"),
            "teaser": desc[:600],
            "cat": f["cat"],
            "lang": f.get("lang") or ("en" if re.search(r"\b(the|and|of|to)\b", title.lower()) else "de"),
            "feed": f["name"] if not f.get("q") else f"Google News: {f['q']}",
        })
    return items


def load_feeds(cfg):
    status, out = [], []

    def one(f):
        url = feed_url(f)
        try:
            raw, _ = http_get(url, timeout=20, accept="application/rss+xml, application/xml, text/xml")
            its = parse_feed(raw, f)
            return f, its, None
        except Exception as e:
            return f, [], str(e)[:160]

    with ThreadPoolExecutor(max_workers=8) as ex:
        for f, its, err in ex.map(one, cfg["feeds"]):
            name = f["name"] if not f.get("q") else f"Google News: {f['q']}"
            status.append({"feed": name, "cat": f["cat"], "ok": err is None, "count": len(its), "error": err})
            out.extend(its)
            log(f"Feed {name}: {'OK ' + str(len(its)) if err is None else 'FEHLER ' + err}")
    return out, status


# ----------------------------------------------------------------------------- Auswertung ohne KI
SPORT = re.compile(r"\b(bundesliga|fußball|fussball|champions league|tor(e)?\b|trainer|handball|formel 1|"
                   r"tennis|olympi|nba|nfl|premier league|wm-qualifikation|em-qualifikation|biathlon|"
                   r"skispringen|dfb-pokal|tatort|horoskop|lotto|rezept|wetter)\b", re.I)

TOPICS = {
    "Zinsen & Notenbanken": r"\b(zins|leitzins|ezb|fed\b|federal reserve|notenbank|lagarde|powell|rate cut|rate hike|interest rate|geldpolitik|bund(e)?s?anleihe|rendite|anleihe|treasur)",
    "Inflation": r"\b(inflation|verbraucherpreis|teuerung|cpi\b|pce\b|preisdruck)",
    "Konjunktur": r"\b(konjunktur|bip\b|gdp|wachstum|rezession|recession|ifo|zew|einkaufsmanager|pmi|arbeitsmarkt|arbeitslos|jobs report|payrolls|auftragseingang|industrieproduktion)",
    "Handel & Zölle": r"\b(zoll|zölle|tariff|handelsstreit|handelskonflikt|export|import|sanktion|lieferkette)",
    "Politik & Regierung": r"\b(bundesregierung|kanzler|koalition|bundestag|haushalt|schuldenbremse|wahl|election|regierung|minister|präsident|white house|weißes haus|kongress|eu-kommission|brüssel|gesetz)",
    "Geopolitik": r"\b(krieg|ukraine|russland|nahost|israel|iran|china|taiwan|nato|konflikt|angriff|waffenruhe)",
    "Energie & Rohstoffe": r"\b(öl\b|ölpreis|brent|opec|gas\b|gaspreis|energie|strompreis|gold|kupfer|rohstoff)",
    "Technologie & KI": r"\b(ki\b|künstliche intelligenz|\bai\b|chip|halbleiter|nvidia|semiconductor|software|cloud|tech)",
    "Banken & Finanzen": r"\b(bank|banken|kredit|versicherer|finanzaufsicht|bafin|fintech|börsengang|ipo)",
    "Unternehmen & Bilanzen": r"\b(quartal|bilanz|umsatz|gewinn|prognose|earnings|revenue|guidance|dividende|übernahme|fusion|merger|acquisition|aktienrückkauf)",
    "Krypto": r"\b(bitcoin|krypto|crypto|ethereum|blockchain)",
    "Immobilienpreise": r"\b(immobilienpreis|kaufpreis|hauspreis|wohnungspreis|house price|home price|preisindex|vdp|europace|greix)",
    "Mieten & Wohnen": r"\b(miete|mieten|mietpreis|wohnungsbau|wohnungsmarkt|baugenehmigung|neubau|rent\b|rents\b|housing)",
    "Finanzierung & Bauzinsen": r"\b(bauzins|baufinanzierung|hypothek|mortgage|immobilienkredit|darlehen)",
    "Immobilienbewertung": r"\b(bewertung|gutacht|verkehrswert|marktwert|bodenrichtwert|immowertv|sachverständig|valuation|appraisal|rics|beleihungswert|ertragswert|sachwert|vergleichswert|cap rate|yield)",
    "Gewerbeimmobilien": r"\b(gewerbeimmobilie|büro|office|logistik|einzelhandelsimmobilie|hotelimmobilie|commercial real estate|\bcre\b|leerstand|vacancy)",
    "Fonds & Transaktionen": r"\b(immobilienfonds|offene fonds|spezialfonds|reit|transaktion|investmentmarkt|asset manag|portfolio|fondsmanager|kapitalverwaltung|kvg|ankauf|verkauf|deal)",
    "Nachhaltigkeit / ESG": r"\b(esg|nachhaltig|klimaschutz|energieeffizienz|gebäudeenergie|heizungsgesetz|co2|taxonomie)",
}
POS = r"\b(steig|stieg|zieh\w* (wieder )?an|legt\w* zu|gewinn|plus\b|rekord|hoch\b|erhol|übertr|stark|aufschwung|rally|surge|soar|jump|rise|rose|gain|beat|record|boost|optimis|wachst|zuversicht|entspann|senkt (die )?zinsen)"
NEG = r"\b(fall|fiel|sinkt|sank|verlust|minus\b|einbruch|absturz|krise|warn|schwach|rezession|insolvenz|pleite|streik|crash|plunge|slump|drop|fell|loss|miss|weak|fear|sorge|risik|unsicher|abschwung|kürzt|stellenabbau|zoll|sanktion|krieg)"
FIG = re.compile(r"([+\-–]?\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?\s?(?:%|prozent|percent|basispunkte|bp|mrd\.?|milliarden|mio\.?|millionen|billion|million|euro|€|dollar|\$|punkte))", re.I)


def rule_analysis(it):
    text = f"{it['title']} {it.get('teaser', '')}"
    low = text.lower()
    tags = [k for k, rx in TOPICS.items() if re.search(rx, low)]
    p, n = len(re.findall(POS, low)), len(re.findall(NEG, low))
    mood = "positiv" if p > n else "negativ" if n > p else "neutral"
    figs = []
    for m in FIG.findall(text):
        m = m.strip()
        if m not in figs:
            figs.append(m)
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", it.get("teaser", "")) if len(s.strip()) > 30]
    key = sorted(sents, key=lambda s: -(len(FIG.findall(s)) * 2 + sum(1 for rx in TOPICS.values() if re.search(rx, s.lower()))))[:3]
    return {"tags": tags[:5], "mood": mood, "figures": figs[:6], "keypoints": key}


def cluster(items):
    """Gleiche Meldung aus mehreren Quellen zusammenfassen."""
    items.sort(key=lambda x: x["published"])
    out = []
    for it in items:
        tok = norm_tokens(it["title"])
        hit = None
        for c in out[-400:]:
            ct = c["_tok"]
            if not tok or not ct:
                continue
            inter = len(tok & ct)
            jac = inter / max(1, len(tok | ct))
            overlap = inter / max(1, min(len(tok), len(ct)))
            # gleicher Titel (fast) – oder gleiche Meldung anders formuliert aus einer ANDEREN Quelle
            if jac >= 0.55 or (inter >= 4 and overlap >= 0.7 and it["source"] != c["source"]):
                hit = c
                break
        if hit:
            src = it["source"]
            if src != hit["source"] and src not in [a["source"] for a in hit["also"]]:
                hit["also"].append({"source": src, "link": it["link"]})
            if len(it.get("teaser", "")) > len(hit.get("teaser", "")):
                hit["teaser"] = it["teaser"]
            if it["cat"] not in hit["cats"]:
                hit["cats"].append(it["cat"])
        else:
            it = dict(it)
            it["_tok"] = tok
            it["also"] = []
            it["cats"] = [it["cat"]]
            out.append(it)
    return out


def score(it):
    age_h = (NOW - parse_date(it["published"])).total_seconds() / 3600
    s = 10 - min(age_h, 72) / 7.2
    s += 3 * len(it["also"])
    s += 1.2 * len(it["auto"]["tags"])
    s += 0.5 * len(it["auto"]["figures"])
    if it.get("ai") and it["ai"].get("relevanz"):
        s += 2.5 * (int(it["ai"]["relevanz"]) - 3)
    return round(s, 2)


# ----------------------------------------------------------------------------- Märkte
CNBC_MAP = {"^GDAXI": ".GDAXI", "^MDAXI": ".MDAXI", "^STOXX50E": ".STOXX50E", "^FTSE": ".FTSE", "^GSPC": ".SPX",
            "^NDX": ".NDX", "^IXIC": ".IXIC", "^DJI": ".DJI", "^VIX": ".VIX", "ES=F": "@SP.1", "NQ=F": "@ND.1",
            "^N225": ".N225", "^HSI": ".HSI", "000001.SS": ".SSEC", "EURUSD=X": "EUR=", "GC=F": "@GC.1",
            "BZ=F": "@LCO.1", "^TNX": "US10Y", "BTC-EUR": "BTC.CB=", "URTH": "URTH", "EUNL.DE": "EUNL-DE"}


class Yahoo:
    """Yahoo Finance mit Browser-Cookie und 'Crumb' – ohne das blockt Yahoo Server-Anfragen oft mit HTTP 429."""

    def __init__(self):
        import http.cookiejar
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.op.addheaders = [("User-Agent", UA), ("Accept", "*/*"), ("Accept-Language", "de-DE,de;q=0.9,en;q=0.8")]
        self.crumb = None
        for url in ("https://fc.yahoo.com", "https://finance.yahoo.com/"):
            try:
                self.op.open(url, timeout=15).read(200)
            except Exception:
                pass  # fc.yahoo.com antwortet absichtlich mit 404, setzt aber das Cookie
        try:
            c = self.op.open("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=15).read().decode().strip()
            if c and "<" not in c and len(c) < 40:
                self.crumb = c
        except Exception as e:
            log(f"Yahoo-Crumb nicht erhalten: {str(e)[:80]}")

    def chart(self, sym):
        last = None
        for host in ("query2", "query1"):
            url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range=1mo&interval=1d"
            if self.crumb:
                url += "&crumb=" + urllib.parse.quote(self.crumb)
            for attempt in range(2):
                try:
                    with self.op.open(url, timeout=20) as r:
                        return json.loads(r.read().decode())
                except urllib.error.HTTPError as e:
                    last = e
                    if e.code == 429:
                        time.sleep(3 + 4 * attempt)
                        continue
                    break
                except Exception as e:
                    last = e
                    break
        raise last


def parse_yahoo(m, j):
    r = j["chart"]["result"][0]
    meta = r["meta"]
    ts = r.get("timestamp") or []
    closes = (r.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    pts = [(t, c) for t, c in zip(ts, closes) if c is not None]
    price = meta.get("regularMarketPrice") or (pts[-1][1] if pts else None)
    mtime = meta.get("regularMarketTime") or (pts[-1][0] if pts else None)
    prev = None
    if pts:
        last_day = dt.datetime.fromtimestamp(pts[-1][0], dt.timezone.utc).date()
        mday = dt.datetime.fromtimestamp(mtime, dt.timezone.utc).date() if mtime else last_day
        prev = pts[-2][1] if (last_day == mday and len(pts) > 1) else pts[-1][1]
    first = pts[0][1] if pts else None
    return {**m, "price": price, "prev": prev,
            "chgPct": round((price / prev - 1) * 100, 2) if price and prev else None,
            "chg1mPct": round((price / first - 1) * 100, 2) if price and first else None,
            "currency": meta.get("currency"), "time": mtime,
            "spark": [round(c, 4) for _, c in pts][-23:], "ok": True, "src": "Yahoo Finance"}


def cnbc_quotes(syms):
    """Ersatzquelle ohne Schlüssel (CNBC-Kursdienst)."""
    out = {}
    if not syms:
        return out
    q = "|".join(syms)
    url = ("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol?symbols="
           + urllib.parse.quote(q, safe="|") + "&requestMethod=itv&noform=1&partnerId=2&fund=1&exthrs=1&output=json&events=1")
    try:
        j = http_json(url, timeout=20)
        res = (j.get("FormattedQuoteResult") or {}).get("FormattedQuote") or []
        if isinstance(res, dict):
            res = [res]
        num = lambda v: float(str(v).replace(",", "").replace("%", "").replace("+", "")) if v not in (None, "", "UNCH") else None
        for x in res:
            try:
                price = num(x.get("last"))
                prev = num(x.get("previous_day_closing"))
                pct = num(x.get("change_pct"))
                if price is None:
                    continue
                if pct is None and prev:
                    pct = (price / prev - 1) * 100
                out[x.get("symbol")] = {"price": price, "prev": prev, "chgPct": round(pct, 2) if pct is not None else None,
                                        "currency": x.get("currencyCode"), "time": int(NOW.timestamp())}
            except Exception:
                continue
    except Exception as e:
        log(f"CNBC-Kurse nicht abrufbar: {str(e)[:100]}")
    return out


def load_markets(cfg, prev=None):
    prev_by = {m["sym"]: m for m in (prev or {}).get("markets", []) if m.get("ok") and m.get("price") is not None}
    out = {}
    try:
        yh = Yahoo()
    except Exception as e:
        yh = None
        log(f"Yahoo nicht erreichbar: {e}")
    if yh:
        for m in cfg["markets"]:
            try:
                out[m["sym"]] = parse_yahoo(m, yh.chart(m["sym"]))
            except Exception as e:
                out[m["sym"]] = {**m, "ok": False, "error": str(e)[:120]}
            time.sleep(0.4)
    # Ersatz: CNBC
    missing = [m for m in cfg["markets"] if not (out.get(m["sym"]) or {}).get("ok")]
    cmap = {m["sym"]: (m.get("cnbc") or CNBC_MAP.get(m["sym"])) for m in missing}
    cq = cnbc_quotes([c for c in cmap.values() if c])
    for m in missing:
        q = cq.get(cmap.get(m["sym"]))
        if q:
            old = prev_by.get(m["sym"], {})
            spark = list(old.get("spark") or [])
            # Verlauf weiterführen: einen Punkt pro Tag
            if not spark or old.get("sparkDay") != NOW.date().isoformat():
                spark.append(q["price"])
            else:
                spark[-1] = q["price"]
            spark = spark[-23:]
            out[m["sym"]] = {**m, **q, "spark": spark, "sparkDay": NOW.date().isoformat(),
                             "chg1mPct": round((q["price"] / spark[0] - 1) * 100, 2) if len(spark) > 5 and spark[0] else old.get("chg1mPct"),
                             "ok": True, "src": "CNBC"}
    # letzter Ausweg: letzte bekannte Werte weiter anzeigen
    for m in cfg["markets"]:
        if not (out.get(m["sym"]) or {}).get("ok") and m["sym"] in prev_by:
            out[m["sym"]] = {**prev_by[m["sym"]], **m, "stale": True}
    res = []
    for m in cfg["markets"]:
        r = out.get(m["sym"]) or {**m, "ok": False, "error": "keine Quelle"}
        if r.get("ok") and not r.get("stale"):
            r.pop("stale", None)
        res.append(r)
        log(f"Markt {m['name']}: {('OK ' + r.get('src', '') + (' (alter Stand)' if r.get('stale') else '')) if r.get('ok') else 'FEHLER ' + r.get('error', '')}")
    return res


def fmt_num(v, dec=2):
    s = f"{v:,.{dec}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


# ----------------------------------------------------------------------------- KI
class AI:
    """KI-Zugang (OpenAI-kompatible Schnittstelle, Standard: Google Gemini).
    Zählt Aufrufe je Modell und Tag; ein Modell mit erschöpftem Kontingent wird für den Lauf gesperrt."""

    def __init__(self, cfg, state):
        a = cfg.get("ai", {})
        self.key = os.environ.get("AI_API_KEY", "").strip()
        self.url = os.environ.get("AI_BASE_URL") or a.get("baseUrl")
        self.model = os.environ.get("AI_MODEL") or a.get("model")
        self.dossier_model = os.environ.get("AI_DOSSIER_MODEL") or a.get("dossierModel") or self.model
        self.max_day = int(a.get("maxCallsPerDay", 40))
        self.max_dossier_day = int(a.get("dossierMaxCallsPerDay", 12))
        today = berlin_now().date().isoformat()
        if state.get("aiDate") != today:
            state["aiDate"], state["aiCalls"], state["aiByModel"] = today, 0, {}
        state.setdefault("aiByModel", {})
        self.state = state
        self.blocked = {}
        self.enabled = bool(self.key and self.url and self.model)
        self.disabled_reason = None if self.enabled else "kein KI-Schlüssel hinterlegt"

    def used(self, model):
        return self.state["aiByModel"].get(model, 0)

    def budget(self, model=None):
        model = model or self.model
        cap = self.max_dossier_day if (model == self.dossier_model and model != self.model) else self.max_day
        return cap - self.used(model)

    def usable(self, model=None):
        model = model or self.model
        return self.enabled and model not in self.blocked and self.budget(model) > 0

    def chat(self, system, user, max_tokens=4000, model=None, timeout=120):
        """Ein KI-Aufruf. Bei Überlastung (503/500/Zeitüberschreitung) wird bis zu 3-mal mit Pause wiederholt.
        Nur erfolgreiche Aufrufe zählen gegen das Tageslimit."""
        model = model or self.model
        if not self.usable(model):
            return None
        self.last_error = None
        body = json.dumps({"model": model, "temperature": 0.25, "max_tokens": max_tokens,
                           "messages": [{"role": "system", "content": system},
                                        {"role": "user", "content": user}]}).encode()
        for attempt in range(4):
            req = urllib.request.Request(self.url, data=body, method="POST", headers={
                "Content-Type": "application/json", "Authorization": f"Bearer {self.key}"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    j = json.loads(r.read().decode())
                self.state["aiByModel"][model] = self.used(model) + 1
                self.state["aiCalls"] = self.state.get("aiCalls", 0) + 1
                self.last_model = model
                ch = j["choices"][0]
                if ch.get("finish_reason") == "length":
                    log(f"Hinweis: Antwort von {model} wurde wegen Längenbegrenzung abgeschnitten")
                return ch["message"]["content"]
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "replace")[:300]
                self.last_error = f"HTTP {e.code}"
                if e.code in (500, 502, 503, 504) and attempt < 3:
                    wait = (15, 40, 90)[attempt]
                    log(f"KI {model} überlastet (HTTP {e.code}) – neuer Versuch in {wait} s")
                    time.sleep(wait)
                    continue
                log(f"KI-Fehler HTTP {e.code} ({model}): {msg}")
                if e.code in (401, 403):
                    self.enabled, self.disabled_reason = False, f"Schlüssel abgelehnt (HTTP {e.code})"
                elif e.code == 429:
                    self.blocked[model] = "Kontingent erreicht (HTTP 429)"
                    if model == self.model:
                        self.disabled_reason = "Tageskontingent der KI erreicht (HTTP 429)"
                elif e.code == 404:
                    repl = self.replacement(model, msg)
                    if repl and repl != model and not getattr(self, "_retrying", False):
                        log(f"Modell {model} nicht verfügbar – wechsle zu {repl}")
                        if model == self.model:
                            self.model = repl
                        if model == self.dossier_model:
                            self.dossier_model = repl
                        self._retrying = True
                        try:
                            return self.chat(system, user, max_tokens, repl, timeout)
                        finally:
                            self._retrying = False
                    self.blocked[model] = "Modell nicht gefunden (HTTP 404)"
                    if model == self.model:
                        self.disabled_reason = f"Modell {model} nicht verfügbar"
                elif e.code in (500, 502, 503, 504):
                    self.blocked[model] = f"derzeit überlastet (HTTP {e.code})"
                return None
            except Exception as e:
                self.last_error = str(e)[:80]
                if attempt < 2:
                    log(f"KI {model}: {e} – neuer Versuch")
                    time.sleep(15)
                    continue
                log(f"KI-Fehler ({model}): {e}")
                return None
        return None

    def replacement(self, model, msg=""):
        """Nachfolgemodell finden: zuerst den Vorschlag aus der Fehlermeldung, sonst die Modellliste des Anbieters."""
        m = re.search(r"use models/([\w.\-]+)", msg or "")
        if m:
            return m.group(1)
        try:
            base = self.url.rsplit("/chat/completions", 1)[0]
            req = urllib.request.Request(base + "/models", headers={"Authorization": f"Bearer {self.key}"})
            with urllib.request.urlopen(req, timeout=20) as r:
                ids = [x.get("id", "").replace("models/", "") for x in json.loads(r.read().decode()).get("data", [])]
            want_lite = "lite" in model
            cands = [i for i in ids if "flash" in i and ("lite" in i) == want_lite
                     and not re.search(r"preview|exp|tts|image|audio|live|embed", i)]
            ver = lambda i: [int(x) for x in re.findall(r"\d+", i)[:2]] or [0]
            return sorted(cands, key=ver)[-1] if cands else None
        except Exception as e:
            log(f"Modellliste nicht abrufbar: {e}")
            return None

    @property
    def active(self):
        return self.enabled and self.model not in self.blocked


def parse_json_loose(txt):
    if not txt:
        return None
    t = txt.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    for opener, closer in (("[", "]"), ("{", "}")):
        i, j = t.find(opener), t.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except Exception:
                continue
    return None


SYS_ARTICLES = """Du bist Analyst für Finanzmärkte, Wirtschaftspolitik und Immobilien (Bewertung und Asset Management) und schreibst für eine Leserin in Deutschland.
Werte jede Meldung sachlich aus. Schreibe IMMER auf Deutsch, auch bei englischen Quellen, in eigenen Worten (keine wörtlichen Zitate).
Erfinde keine Fakten: Nutze nur, was in Titel, Anriss oder Text steht. Wenn nur wenig Information vorliegt, bleib kurz und sag das.
Antworte ausschließlich mit einem JSON-Array, ein Objekt pro Meldung:
{"id": "...", "zusammenfassung": "3–5 Sätze", "kernpunkte": ["…", "…", "…"], "einordnung": "1–2 Sätze: Was bedeutet das für Märkte, Zinsen, Immobilien oder Anleger?", "stimmung": "positiv|neutral|negativ", "relevanz": 1-5, "themen": ["…"]}"""

SYS_BRIEFING = """Du bist Chefredakteurin eines morgendlichen Finanz-, Wirtschafts- und Immobilien-Briefings für eine fachkundige Leserin in Deutschland.
Schreibe auf Deutsch, präzise und konkret, in eigenen Worten, ohne Anlageempfehlungen.
Erfinde nichts: Nutze nur die gelieferten Dossiers, Meldungen und Marktdaten. Jede Aussage soll KONKRET sein – mit Zahlen, Prozenten, Beträgen, Daten, Uhrzeiten, Orten, Namen und Beispielen, soweit sie in den Quellen stehen. Keine allgemeinen Floskeln.
Termine: Nenne Datum (TT.MM.JJJJ mit Wochentag), Uhrzeit (deutsche Zeit, falls in den Quellen genannt), was genau passiert, wo bzw. wie (z. B. „Quartalszahlen per Pressemitteilung nach US-Börsenschluss, anschließend Telefonkonferenz/Webcast für Analysten auf der Investor-Relations-Seite“) und was erwartet wird (z. B. Analystenschätzungen mit Zahlen). Relative Angaben wie „am Mittwoch“ rechnest du anhand des Erscheinungsdatums der Quelle in ein Datum um. Steht etwas nicht in den Quellen, schreibe „laut Quellen nicht genannt“ statt zu raten.
Antworte ausschließlich mit JSON in genau dieser Struktur:
{"titel": "prägnante Überschrift des Tages",
 "kurz": ["5–8 Stichpunkte: die wichtigsten Entwicklungen, jeder mit mindestens einer konkreten Zahl oder Tatsache"],
 "maerkte": "Absatz: Was bewegt die Aktienmärkte – mit Indexständen und Veränderungen aus den Marktdaten und konkreten Beispielen (Einzelwerte, Branchen, Gründe)",
 "abschnitte": [{"kategorie": "<Kategorie-ID>", "text": "4–6 Sätze mit konkreten Fakten, Zahlen und Beispielen", "zahlen": ["Kennzahl: Wert (Kontext)", "…"], "ids": ["Artikel-IDs, auf die du dich stützt"]}],
 "termine": [{"wann": "Wochentag, TT.MM.JJJJ, Uhrzeit", "was": "Ereignis", "wo": "wo bzw. wie es stattfindet/veröffentlicht wird", "erwartung": "was erwartet wird, mit Zahlen", "warum": "warum es wichtig ist", "ids": ["Artikel-IDs"]}],
 "beobachten": ["3–6 weitere konkrete Punkte, worauf heute und in den nächsten Tagen zu achten ist"]}
Liefere für JEDE gelieferte Kategorie einen Abschnitt. Sortiere die Termine chronologisch."""


def ai_articles(ai, items, cfg):
    a = cfg.get("ai", {})
    todo = [it for it in items if not it.get("ai")]
    todo.sort(key=lambda x: -x["score"])
    todo = todo[: int(a.get("maxArticlesPerRun", 32))]
    per = int(a.get("articlesPerCall", 8))
    if not todo or not ai.usable():
        return 0
    if a.get("fetchFullText", True):
        with ThreadPoolExecutor(max_workers=6) as ex:
            texts = list(ex.map(lambda it: fetch_fulltext(it["link"]), todo))
        for it, t in zip(todo, texts):
            it["_full"] = t
    done = 0
    for i in range(0, len(todo), per):
        if not ai.usable() or ai.budget() <= 1:  # 1 Aufruf für den Morgenbericht freihalten
            break
        batch = todo[i:i + per]
        parts = []
        for it in batch:
            body = it.get("_full") or it.get("teaser") or "(nur Schlagzeile verfügbar)"
            parts.append(f"### id: {it['id']}\nQuelle: {it['source']} ({it['published'][:16]})\nTitel: {it['title']}\nText: {body[:3500]}")
        res = parse_json_loose(ai.chat(SYS_ARTICLES, "\n\n".join(parts)))
        if isinstance(res, dict):
            res = res.get("artikel") or res.get("items") or [res]
        if not isinstance(res, list):
            continue
        by_id = {it["id"]: it for it in batch}
        for r in res:
            it = by_id.get(str(r.get("id", "")))
            if not it:
                continue
            try:
                rel = max(1, min(5, int(r.get("relevanz", 3))))
            except Exception:
                rel = 3
            it["ai"] = {
                "zusammenfassung": str(r.get("zusammenfassung", ""))[:1500],
                "kernpunkte": [str(x)[:300] for x in (r.get("kernpunkte") or [])][:5],
                "einordnung": str(r.get("einordnung", ""))[:700],
                "stimmung": r.get("stimmung") if r.get("stimmung") in ("positiv", "neutral", "negativ") else None,
                "relevanz": rel,
                "themen": [str(x)[:40] for x in (r.get("themen") or [])][:5],
                "basis": "Volltext" if it.get("_full") else "Anriss",
                "model": ai.model,
                "at": NOW.isoformat(timespec="seconds"),
            }
            done += 1
    for it in todo:
        it.pop("_full", None)
    return done


def market_lines(markets):
    lines = []
    for m in markets:
        if not m.get("ok") or m.get("price") is None:
            continue
        ch = f"{m['chgPct']:+.2f} %".replace(".", ",") if m.get("chgPct") is not None else "–"
        lines.append(f"{m['name']}: {fmt_num(m['price'])} ({ch} zum Vortag)")
    return lines


def rule_briefing(items, markets, cfg):
    cats = {c["id"]: c["name"] for c in cfg["categories"]}
    top = sorted(items, key=lambda x: -x["score"])
    ok = [m for m in markets if m.get("ok") and m.get("chgPct") is not None]
    def pick(names):
        return [m for m in ok if m["name"] in names]
    mk = []
    for grp in ("Asien", "Europa", "USA", "Futures"):
        g = [m for m in ok if m["group"] == grp and "VIX" not in m["name"]]
        if g:
            mk.append(f"{grp}: " + ", ".join(f"{m['name']} {m['chgPct']:+.1f} %".replace(".", ",") for m in g))
    sections, used = [], set()
    for cid, cname in cats.items():
        its = [x for x in top if cid in x["cats"] and x["id"] not in used][:3]
        used.update(x["id"] for x in its)
        if its:
            sections.append({"kategorie": cid, "text": " · ".join(x["title"] for x in its), "ids": [x["id"] for x in its]})
    return {
        "titel": "Dein Überblick am " + berlin_now().strftime("%d.%m.%Y"),
        "ueberblick": "Die meistbeachteten Meldungen: " + " · ".join(x["title"] for x in top[:5]) if top else "Noch keine Meldungen geladen.",
        "maerkte": ". ".join(mk) + "." if mk else "",
        "abschnitte": sections,
        "beobachten": [],
        "art": "automatisch",
        "at": NOW.isoformat(timespec="seconds"),
    }


def ai_briefing(ai, items, markets, cfg, dossiers=None):
    model = ai.dossier_model if ai.usable(ai.dossier_model) else ai.model
    if not ai.usable(model):
        return None
    since = NOW - dt.timedelta(hours=30)
    recent = [x for x in items if parse_date(x["published"]) >= since] or items
    recent = sorted(recent, key=lambda x: -x["score"])
    per_cat = {}
    for x in recent:
        for c in x["cats"]:
            per_cat.setdefault(c, [])
            if len(per_cat[c]) < 10 and x not in per_cat[c]:
                per_cat[c].append(x)
    cats = {c["id"]: c["name"] for c in cfg["categories"]}
    dos = {d["topic"]: d for d in (dossiers or []) if d.get("datum") == berlin_now().date().isoformat()}
    blocks = [f"HEUTE: {berlin_now().strftime('%A, %d.%m.%Y, %H:%M')} Uhr (deutsche Zeit)",
              "MARKTDATEN:\n" + "\n".join(market_lines(markets))]
    for cid in cats:
        its = per_cat.get(cid, [])
        if not its and cid not in dos:
            continue
        lines = []
        for x in its:
            summ = (x.get("ai") or {}).get("zusammenfassung") or x.get("teaser", "")
            lines.append(f"- id {x['id']} | {x['source']} | erschienen {x['published'][:16].replace('T', ' ')} UTC | {x['title']} | {summ[:600]}")
        block = f"KATEGORIE {cid} ({cats[cid]}):\n" + "\n".join(lines)
        if cid in dos:
            block += f"\n\nDOSSIER ZU DIESEM THEMA (ausführlich, als Hauptquelle nutzen):\n{dos[cid]['body'][:24000]}"
        blocks.append(block)
    res = parse_json_loose(ai.chat(SYS_BRIEFING, "\n\n".join(blocks), max_tokens=12000, model=model, timeout=300))
    if not isinstance(res, dict) or not (res.get("kurz") or res.get("ueberblick")):
        return None
    valid = {x["id"] for x in items}
    secs = []
    for s_ in res.get("abschnitte") or []:
        if isinstance(s_, dict) and s_.get("text"):
            secs.append({"kategorie": s_.get("kategorie"), "text": str(s_["text"])[:2000],
                         "zahlen": [str(z)[:200] for z in (s_.get("zahlen") or [])][:8],
                         "ids": [i for i in (s_.get("ids") or []) if i in valid][:6]})
    termine = []
    for t in res.get("termine") or []:
        if isinstance(t, dict) and (t.get("was") or t.get("wann")):
            termine.append({k: str(t.get(k, ""))[:400] for k in ("wann", "was", "wo", "erwartung", "warum")}
                           | {"ids": [i for i in (t.get("ids") or []) if i in valid][:4]})
    kurz = [str(k)[:400] for k in (res.get("kurz") or [])][:10]
    return {"titel": str(res.get("titel", ""))[:200], "kurz": kurz,
            "ueberblick": str(res.get("ueberblick") or " ".join(kurz[:2]))[:2000],
            "maerkte": str(res.get("maerkte", ""))[:2500], "abschnitte": secs, "termine": termine[:12],
            "beobachten": [str(b_)[:400] for b_ in (res.get("beobachten") or [])][:8],
            "dossiers": len(dos), "art": "ki", "model": getattr(ai, "last_model", model),
            "at": NOW.isoformat(timespec="seconds")}


# ----------------------------------------------------------------------------- Google-News-Links auflösen
def gnews_resolve(link, cache):
    """Google-News-Links zeigen nicht direkt auf den Artikel. Hier wird – wie die Google-News-Seite
    selbst – die eigentliche Verlags-Adresse erfragt. Gelingt das nicht, bleibt es beim Google-Link."""
    if "news.google.com" not in (link or ""):
        return link
    if link in cache:
        return cache[link]
    try:
        m = re.search(r"/articles/([^?/]+)", link)
        if not m:
            return None
        gid = m.group(1)
        raw, _ = http_get(f"https://news.google.com/articles/{gid}?hl=de&gl=DE&ceid=DE:de", timeout=15, accept="text/html", tries=1)
        page = raw.decode("utf-8", "replace")
        sg = re.search(r'data-n-a-sg="([^"]+)"', page)
        ts = re.search(r'data-n-a-ts="([^"]+)"', page)
        if not (sg and ts):
            cache[link] = None
            return None
        inner = ('["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
                 '"X","X",1,[1,1,1],1,1,null,0,0,null,0],"%s",%s,"%s"]') % (gid, ts.group(1), sg.group(1))
        freq = json.dumps([[["Fbv4je", inner, None, "generic"]]])
        data = urllib.parse.urlencode({"f.req": freq}).encode()
        req = urllib.request.Request("https://news.google.com/_/DotsSplashUi/data/batchexecute", data=data, method="POST",
                                     headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
        with urllib.request.urlopen(req, timeout=15) as r:
            txt = r.read().decode("utf-8", "replace")
        part = txt.split("\n\n", 1)[1]
        arr = json.loads(part)
        url = json.loads(arr[0][2])[1]
        if isinstance(url, str) and url.startswith("http"):
            cache[link] = url
            return url
    except Exception as e:  # noqa
        log(f"Google-News-Link nicht auflösbar: {str(e)[:80]}")
    cache[link] = None
    return None


# ----------------------------------------------------------------------------- Wikipedia (Hintergrund)
WIKI_UA = "MorgenBriefing/1.0 (private Nachrichten-App; Python urllib)"


def wiki_extract(term, lang="de", chars=1800):
    """Einleitung eines Wikipedia-Artikels (frei, ohne Schlüssel)."""
    try:
        q = urllib.parse.urlencode({"action": "query", "format": "json", "prop": "extracts|info", "inprop": "url",
                                    "exintro": 1, "explaintext": 1, "redirects": 1, "titles": term})
        req = urllib.request.Request(f"https://{lang}.wikipedia.org/w/api.php?{q}", headers={"User-Agent": WIKI_UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            j = json.loads(r.read().decode())
        for pg in (j.get("query", {}).get("pages") or {}).values():
            ext = (pg.get("extract") or "").strip()
            if ext and "missing" not in pg:
                return {"title": pg.get("title", term), "url": pg.get("fullurl") or f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(term)}",
                        "text": ext[:chars]}
    except Exception as e:
        log(f"Wikipedia {term}: {str(e)[:80]}")
    return None


def wiki_search(term, lang="de"):
    try:
        q = urllib.parse.urlencode({"action": "query", "format": "json", "list": "search", "srsearch": term, "srlimit": 1})
        req = urllib.request.Request(f"https://{lang}.wikipedia.org/w/api.php?{q}", headers={"User-Agent": WIKI_UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            j = json.loads(r.read().decode())
        hits = j.get("query", {}).get("search") or []
        return hits[0]["title"] if hits else None
    except Exception:
        return None


def hot_terms(items, n=3):
    """Begriffe, die in vielen Schlagzeilen eines Themas vorkommen (für zusätzlichen Hintergrund)."""
    cnt = {}
    for it in items:
        for w in set(re.findall(r"\b[A-ZÄÖÜ][\wäöüß\-]{3,}\b", it["title"])):
            if w.lower() in ("der", "die", "das", "wie", "was", "warum", "neue", "neuer", "mehr", "nach", "beim", "this", "with", "from", "what", "will", "stock", "stocks", "market", "markets"):
                continue
            cnt[w] = cnt.get(w, 0) + 1
    return [w for w, c in sorted(cnt.items(), key=lambda x: -x[1]) if c >= 3][:n]


# ----------------------------------------------------------------------------- Themen-Dossiers
SYS_DOSSIER = """Du bist leitende Fachredakteurin für Finanzmärkte, Wirtschaftspolitik und Immobilienwirtschaft (Bewertung, Märkte, Asset Management). Du schreibst für eine fachlich interessierte Leserin in Deutschland ein ausführliches, fundiertes THEMENDOSSIER zum aktuellen Stand.

Arbeitsweise:
- Lies ALLE gelieferten Quellen. Verschaffe dir ein Gesamtbild: Was sind die wichtigsten Entwicklungen, welche Fakten, Zahlen, Beispiele und Zusammenhänge gibt es, wo widersprechen sich Quellen?
- Schreibe einen EIGENEN, neuen Artikel in deinen eigenen Worten. Keine längeren wörtlichen Übernahmen; kurze Zitate (unter 15 Wörtern) nur in Anführungszeichen mit Quelle.
- Nutze ausschließlich Informationen aus den gelieferten Artikeln [1], [2], … und dem Hintergrundwissen [W1], [W2], … Erfinde keine Zahlen, Namen, Daten oder Ereignisse. Allgemeines Fachwissen zur Erklärung von Begriffen ist erlaubt, aber keine aktuellen Fakten ohne Quelle.
- Belege jede konkrete Tatsachenaussage, Zahl und jedes Beispiel direkt im Satz mit der Quellennummer in eckigen Klammern, z. B. [3] oder [2][7] oder [W1].
- Bei widersprüchlichen Angaben: beide nennen und einordnen. Bei dünner Quellenlage: das offen sagen.
- Keine Anlageberatung, keine Kauf-/Verkaufsempfehlungen.

Länge: richte dich nach der Informationsdichte. Mindestens 1000 Wörter. Bei viel Material 3000 bis 6000 Wörter oder mehr. Gründlich, konkret und detailliert, aber ohne Wiederholungen und Füllsätze.

Form (Markdown, keine Tabellen, keine Bilder):
# <prägnanter Titel des Dossiers>
> <Vorspann: 2–3 Sätze, worum es heute geht>
## Das Wichtigste in Kürze
- 5–8 Spiegelstriche mit den zentralen Fakten und Zahlen
## <thematische Kapitel, so viele wie sinnvoll, jeweils mit ### Unterkapiteln>
(Fakten, Zahlen, konkrete Beispiele, Akteure, Ursachen, Zusammenhänge)
## Hintergrund: Begriffe und Zusammenhänge
(Erkläre die wichtigsten Fachbegriffe und Mechanismen, gestützt auf [W…] wo vorhanden)
## Einordnung: Was bedeutet das?
(Folgen für Märkte, Anleger, Zinsen, Immobilienwerte bzw. Asset Management – je nach Thema)
## Ausblick: Worauf jetzt zu achten ist
(nur, was aus den Quellen ableitbar ist: Termine, Daten, offene Fragen)
Antworte nur mit dem Markdown-Text."""


def build_dossier(ai, cat, items, markets, cfg, gcache):
    a = cfg.get("ai", {})
    n_full = int(a.get("dossierArticles", 25))
    n_more = int(a.get("dossierMoreHeadlines", 40))
    max_chars = int(a.get("dossierFulltextChars", 7000))
    pool = sorted([x for x in items if cat["id"] in x["cats"]], key=lambda x: -x["score"])
    if len(pool) < 3:
        log(f"Dossier {cat['name']}: zu wenig Meldungen ({len(pool)})")
        return None
    main_items = pool[:n_full]

    # echte Artikel-Adressen ermitteln und Volltexte lesen
    def prep(it):
        url = it.get("url") or gnews_resolve(it["link"], gcache) or it["link"]
        if url and "news.google.com" not in url:
            it["url"] = url
        txt = fetch_fulltext(url, limit=max_chars) if url and "news.google.com" not in url else ""
        return txt if len(txt) > 400 else ""
    with ThreadPoolExecutor(max_workers=6) as ex:
        texts = list(ex.map(prep, main_items))

    # Hintergrund aus Wikipedia
    terms = list((cfg.get("dossierWiki") or {}).get(cat["id"], []))
    for t in hot_terms(pool):
        hit = wiki_search(t)
        if hit and hit not in terms:
            terms.append(hit)
    wiki = []
    for t in terms[:10]:
        w = wiki_extract(t)
        if w and w["title"] not in [x["title"] for x in wiki]:
            wiki.append(w)

    sources, parts = [], []
    for i, (it, txt) in enumerate(zip(main_items, texts), 1):
        sources.append({"n": i, "title": it["title"], "source": it["source"], "url": it.get("url") or it["link"],
                        "published": it["published"], "id": it["id"], "full": bool(txt)})
        body = txt or it.get("teaser") or "(nur Schlagzeile verfügbar)"
        parts.append(f"[{i}] {it['source']}, {it['published'][:16].replace('T', ' ')} UTC – {it['title']}\n"
                     f"{'VOLLTEXT' if txt else 'ANRISS'}: {body}")
    k = len(main_items)
    for j, it in enumerate(pool[n_full:n_full + n_more], k + 1):
        sources.append({"n": j, "title": it["title"], "source": it["source"], "url": it.get("url") or it["link"],
                        "published": it["published"], "id": it["id"], "full": False})
        parts.append(f"[{j}] {it['source']} – {it['title']}" + (f" – {it['teaser'][:300]}" if it.get("teaser") else ""))
    wparts = [f"[W{i}] Wikipedia: {w['title']}\n{w['text']}" for i, w in enumerate(wiki, 1)]
    mk = ""
    if cat["id"] in ("maerkte", "indizes", "geld", "politik"):
        mk = "AKTUELLE MARKTDATEN (Stand " + NOW.strftime("%d.%m.%Y %H:%M UTC") + "):\n" + "\n".join(
            f"{m['name']}: {fmt_num(m['price'])} ({m['chgPct']:+.2f} % zum Vortag, {m['chg1mPct']:+.2f} % in 1 Monat)"
            for m in markets if m.get("ok") and m.get("price") is not None and m.get("chgPct") is not None and m.get("chg1mPct") is not None)
    user = (f"THEMA: {cat['name']}\nDATUM: {berlin_now().strftime('%A, %d.%m.%Y')}\n\n"
            + (mk + "\n\n" if mk else "")
            + "ARTIKEL:\n\n" + "\n\n".join(parts)
            + ("\n\nHINTERGRUNDWISSEN:\n\n" + "\n\n".join(wparts) if wparts else ""))

    md, used_model = None, None
    for model in (ai.dossier_model, ai.model):
        if ai.usable(model):
            md = ai.chat(SYS_DOSSIER, user, max_tokens=30000, model=model, timeout=420)
            if md:
                log(f"Dossier {cat['name']}: Entwurf mit {len(md.split())} Wörtern ({model})")
            if md and len(md.split()) > 300:
                used_model = getattr(ai, "last_model", model)
                break
            md = None
    if not md:
        cat["_fail"] = "invalid" if ai.last_error is None else "api"
        return None
    # Zu kurz für die Materialmenge? Dann gezielt erweitern lassen.
    min_words = 1000 if len(pool) >= 6 else 600
    if len(md.split()) < min_words and ai.usable(used_model):
        ext = ai.chat(SYS_DOSSIER + "\n\nZUSATZAUFTRAG: Der folgende Entwurf ist zu kurz. Überarbeite und ERWEITERE ihn "
                      "deutlich auf mindestens 1500 Wörter (bei viel Material gern 3000+): mehr konkrete Fakten, Zahlen, Daten, "
                      "Beispiele, Akteure und Zusammenhänge aus den Quellen, jeweils mit Quellennummer. Gib das vollständige Dossier aus.",
                      user + "\n\nBISHERIGER ENTWURF:\n\n" + md, max_tokens=30000, model=used_model, timeout=420)
        if ext and len(ext.split()) > len(md.split()):
            log(f"Dossier {cat['name']}: erweitert auf {len(ext.split())} Wörter")
            md = ext
    md = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", md.strip())
    lines = md.splitlines()
    title, lead, body = cat["name"], [], []
    for ln in lines:
        if ln.startswith("# ") and title == cat["name"] and not body:
            title = ln[2:].strip()
        elif ln.startswith(">") and not body:
            lead.append(ln.lstrip("> ").strip())
        else:
            body.append(ln)
    body_md = "\n".join(body).strip()
    cited = set(int(n) for n in re.findall(r"\[(\d{1,3})\]", body_md))
    wcited = set(int(n) for n in re.findall(r"\[W(\d{1,2})\]", body_md))
    words = len(re.findall(r"\w+", body_md))
    log(f"Dossier {cat['name']}: {words} Wörter, {len(cited)} Artikel zitiert, Modell {used_model}")
    return {"topic": cat["id"], "name": cat["name"], "title": title[:200], "lead": " ".join(lead)[:800],
            "body": body_md, "words": words, "model": used_model, "at": NOW.isoformat(timespec="seconds"),
            "datum": berlin_now().date().isoformat(),
            "sources": [s for s in sources if s["n"] in cited] or sources[:k],
            "wiki": [{"n": i, "title": w["title"], "url": w["url"]} for i, w in enumerate(wiki, 1) if i in wcited],
            "basis": {"articles": len(pool[:n_full + n_more]), "fulltext": sum(1 for t in texts if t), "wiki": len(wiki)}}


def run_dossiers(ai, items, markets, cfg, state, prev):
    """Erzeugt pro Lauf höchstens `dossiersPerRun` fehlende Dossiers des aktuellen Ausgabe-Zeitfensters."""
    a = cfg.get("ai", {})
    bnow = berlin_now()
    hours = sorted(int(h) for h in a.get("dossierHoursBerlin", [4]))
    slots = [h for h in hours if bnow.hour >= h]
    by_topic = {d["topic"]: d for d in (prev or {}).get("items", [])}
    if not ai.enabled or not slots:
        return by_topic
    edition = f"{bnow.date().isoformat()}-{slots[-1]:02d}"
    gcache = state.setdefault("gnews", {})
    todo = [c for c in cfg["categories"] if (by_topic.get(c["id"]) or {}).get("edition") != edition]
    if os.environ.get("EXTRA_FORCE_DOSSIERS") == "1":
        todo = list(cfg["categories"])
    tries = state.setdefault("dossierTries2", {})
    state.pop("dossierTries", None)
    if state.get("triesModel") != ai.dossier_model:  # nach Modellwechsel neue Versuche erlauben
        tries.clear()
        state["triesModel"] = ai.dossier_model
    for k in [k for k in tries if not k.startswith(edition)]:
        tries.pop(k, None)
    done = 0
    for c in todo:
        if done >= int(a.get("dossiersPerRun", 3)):
            break
        if not (ai.usable(ai.dossier_model) or ai.usable(ai.model)):
            break
        key = f"{edition}:{c['id']}"
        if tries.get(key, 0) >= 2:  # nach zwei Fehlversuchen bis zur nächsten Ausgabe pausieren
            continue
        if sum(1 for x in items if c["id"] in x["cats"]) < 3:
            continue
        done += 1
        c = dict(c)
        d = build_dossier(ai, c, items, markets, cfg, gcache)
        if d:
            d["edition"] = edition
            by_topic[c["id"]] = d
        elif c.get("_fail") == "invalid":
            tries[key] = tries.get(key, 0) + 1  # nur unbrauchbare Antworten zählen als Fehlversuch
        if not (ai.usable(ai.dossier_model) or ai.usable(ai.model)):
            break
        time.sleep(5)
    # Zwischenspeicher begrenzen
    if len(gcache) > 4000:
        for k in list(gcache)[:-3000]:
            gcache.pop(k, None)
    return by_topic


# ----------------------------------------------------------------------------- Hauptablauf
def load_prev(name):
    base = os.environ.get("PREV_URL", "").rstrip("/")
    if not base:
        return None
    try:
        return http_json(f"{base}/data/{name}?t={int(time.time())}", timeout=15, tries=1)
    except Exception as e:
        log(f"Vorheriger Stand {name} nicht geladen ({e})")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "_site", "data"))
    ap.add_argument("--offline-test", help="Ordner mit Test-Feeds (nur für Tests)")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))

    prev_news = load_prev("news.json") or {}
    state = load_prev("state.json") or {}
    prev_brief = load_prev("briefing.json")
    prev_dos = load_prev("dossiers.json")
    prev_ai = {x["id"]: x["ai"] for x in prev_news.get("items", []) if x.get("ai")}
    prev_items = prev_news.get("items", [])

    # 1) Nachrichten
    raw, feed_status = load_feeds(cfg)
    max_age = dt.timedelta(hours=cfg.get("newsMaxAgeHours", 72))
    fresh = []
    for it in raw:
        d = parse_date(it["published"])
        if d and d > NOW + dt.timedelta(hours=2):
            it["published"] = NOW.isoformat(timespec="seconds")
            d = NOW
        if not d or NOW - d > max_age:
            continue
        if SPORT.search(it["title"]):
            continue
        fresh.append(it)
    # ältere Meldungen aus dem letzten Lauf behalten (falls eine Quelle gerade ausfällt)
    for p in prev_items:
        d = parse_date(p.get("published"))
        if d and NOW - d <= max_age:
            base = {k: p[k] for k in ("title", "link", "url", "source", "published", "teaser", "lang", "feed") if k in p}
            for c in p.get("cats", [p.get("cat")]):
                if c:
                    fresh.append({**base, "cat": c})
    items = cluster(fresh)
    for it in items:
        it.pop("_tok", None)
        it["id"] = hashlib.sha1((it["link"] or it["title"]).encode()).hexdigest()[:12]
        it["auto"] = rule_analysis(it)
        if it["id"] in prev_ai:
            it["ai"] = prev_ai[it["id"]]
        it["score"] = score(it)
    # doppelte IDs entfernen
    seen, uniq = set(), []
    for it in items:
        if it["id"] not in seen:
            seen.add(it["id"])
            uniq.append(it)
    items = uniq
    log(f"{len(items)} Meldungen nach Zusammenfassung gleicher Themen")

    # 2) Märkte
    markets = load_markets(cfg, load_prev("markets.json"))

    # 3) KI
    ai = AI(cfg, state)
    n_ai = ai_articles(ai, items, cfg)
    log(f"KI-Auswertungen in diesem Lauf: {n_ai} (KI {'aktiv' if ai.active else 'aus: ' + str(ai.disabled_reason)})")
    for it in items:
        it["score"] = score(it)

    # 3b) Themen-Dossiers
    dossiers = run_dossiers(ai, items, markets, cfg, state, prev_dos)
    order = [c["id"] for c in cfg["categories"]]
    dossier_list = sorted(dossiers.values(), key=lambda d: order.index(d["topic"]) if d["topic"] in order else 99)

    # 4) Morgenbericht – einmal täglich ab der eingestellten Stunde (deutsche Zeit) per KI,
    #    sonst automatisch zusammengestellt
    bnow = berlin_now()
    hour = int(cfg.get("ai", {}).get("briefingHourBerlin", 5))
    today = bnow.date().isoformat()
    briefing = None
    have_today = prev_brief and prev_brief.get("datum") == today and prev_brief.get("art") == "ki" and prev_brief.get("kurz")
    force = os.environ.get("EXTRA_FORCE_BRIEFING") == "1"
    n_dos_today = sum(1 for d in dossier_list if d.get("datum") == today)
    # neu schreiben, wenn seit dem letzten Bericht weitere Dossiers fertig geworden sind (max. 4× täglich)
    more_dos = have_today and n_dos_today > int(prev_brief.get("dossiers", 0)) and int(prev_brief.get("regen", 0)) < 4 \
        and (n_dos_today == len(cfg["categories"]) or n_dos_today - int(prev_brief.get("dossiers", 0)) >= 3)
    if (bnow.hour >= hour and (not have_today or more_dos)) or force:
        briefing = ai_briefing(ai, items, markets, cfg, dossier_list)
        if briefing and have_today:
            briefing["regen"] = int(prev_brief.get("regen", 0)) + 1
    if briefing is None and have_today and not force:
        briefing = prev_brief
    if briefing is None:
        briefing = rule_briefing(items, markets, cfg)
    briefing["datum"] = briefing.get("datum") or today
    if briefing.get("art") != "ki":
        briefing["datum"] = today

    # 5) Schreiben
    max_per = cfg.get("newsPerCategory", 60)
    keep, count = [], {}
    for it in sorted(items, key=lambda x: -x["score"]):
        if any(count.get(c, 0) < max_per for c in it["cats"]):
            keep.append(it)
            for c in it["cats"]:
                count[c] = count.get(c, 0) + 1
    keep.sort(key=lambda x: x["published"], reverse=True)

    meta = {
        "updated": NOW.isoformat(timespec="seconds"),
        "ai": {"enabled": ai.active, "reason": ai.disabled_reason, "model": ai.model if ai.key else None,
               "dossierModel": ai.dossier_model if ai.key else None, "blocked": ai.blocked,
               "byModel": state.get("aiByModel", {}),
               "callsToday": state.get("aiCalls", 0), "maxPerDay": ai.max_day},
    }
    def dump(name, obj):
        with open(os.path.join(args.out, name), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    dump("news.json", {**meta, "categories": cfg["categories"], "items": keep})
    dump("markets.json", {**meta, "markets": markets})
    dump("briefing.json", briefing)
    dump("dossiers.json", {"updated": meta["updated"], "items": dossier_list})
    dump("state.json", {**state, "updated": meta["updated"]})
    dump("status.json", {**meta, "feeds": feed_status, "itemCount": len(keep),
                         "marketsOk": sum(1 for m in markets if m.get("ok")), "log": LOG[-80:]})
    log("Fertig.")


if __name__ == "__main__":
    sys.exit(main())
