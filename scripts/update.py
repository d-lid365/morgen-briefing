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
def load_markets(cfg):
    out = []

    def one(m):
        sym = m["sym"]
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range=1mo&interval=1d"
        try:
            j = http_json(url)
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
                if last_day == mday and len(pts) > 1:
                    prev = pts[-2][1]
                else:
                    prev = pts[-1][1]
            first = pts[0][1] if pts else None
            return {**m, "price": price, "prev": prev,
                    "chgPct": round((price / prev - 1) * 100, 2) if price and prev else None,
                    "chg1mPct": round((price / first - 1) * 100, 2) if price and first else None,
                    "currency": meta.get("currency"), "time": mtime,
                    "spark": [round(c, 4) for _, c in pts][-23:], "ok": True}
        except Exception as e:
            return {**m, "ok": False, "error": str(e)[:120]}

    with ThreadPoolExecutor(max_workers=6) as ex:
        for res in ex.map(one, cfg["markets"]):
            out.append(res)
            log(f"Markt {res['name']}: {'OK' if res['ok'] else 'FEHLER ' + res.get('error', '')}")
    return out


def fmt_num(v, dec=2):
    s = f"{v:,.{dec}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


# ----------------------------------------------------------------------------- KI
class AI:
    def __init__(self, cfg, state):
        a = cfg.get("ai", {})
        self.key = os.environ.get("AI_API_KEY", "").strip()
        self.url = os.environ.get("AI_BASE_URL") or a.get("baseUrl")
        self.model = os.environ.get("AI_MODEL") or a.get("model")
        self.max_day = int(a.get("maxCallsPerDay", 40))
        today = berlin_now().date().isoformat()
        if state.get("aiDate") != today:
            state["aiDate"], state["aiCalls"] = today, 0
        self.state = state
        self.enabled = bool(self.key and self.url and self.model)
        self.disabled_reason = None if self.enabled else "kein KI-Schlüssel hinterlegt"

    def budget(self):
        return self.max_day - self.state.get("aiCalls", 0)

    def chat(self, system, user, max_tokens=4000):
        if not self.enabled or self.budget() <= 0:
            return None
        body = json.dumps({"model": self.model, "temperature": 0.2, "max_tokens": max_tokens,
                           "messages": [{"role": "system", "content": system},
                                        {"role": "user", "content": user}]}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.key}"})
        self.state["aiCalls"] = self.state.get("aiCalls", 0) + 1
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                j = json.loads(r.read().decode())
            return j["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:300]
            log(f"KI-Fehler HTTP {e.code}: {msg}")
            if e.code in (401, 403):
                self.enabled, self.disabled_reason = False, f"Schlüssel abgelehnt (HTTP {e.code})"
            if e.code == 429:
                self.enabled, self.disabled_reason = False, "Tageskontingent der KI erreicht (HTTP 429)"
            return None
        except Exception as e:
            log(f"KI-Fehler: {e}")
            return None


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

SYS_BRIEFING = """Du bist Chefredakteurin eines morgendlichen Finanz- und Immobilien-Briefings für eine Leserin in Deutschland.
Schreibe auf Deutsch, klar, ruhig und präzise, in eigenen Worten, ohne Anlageempfehlungen. Erfinde nichts – nutze nur die gelieferten Meldungen und Marktdaten.
Antworte ausschließlich mit JSON:
{"titel": "kurze Überschrift des Tages", "ueberblick": "Absatz mit den 3–5 wichtigsten Entwicklungen", "maerkte": "Absatz: Was bewegt die Aktienmärkte, mit den gelieferten Zahlen",
 "abschnitte": [{"kategorie": "<Kategorie-ID>", "text": "2–4 Sätze", "ids": ["Artikel-IDs, auf die du dich beziehst"]}],
 "beobachten": ["2–4 Punkte, worauf heute/diese Woche zu achten ist (nur aus den Meldungen ableitbar)"]}"""


def ai_articles(ai, items, cfg):
    a = cfg.get("ai", {})
    todo = [it for it in items if not it.get("ai")]
    todo.sort(key=lambda x: -x["score"])
    todo = todo[: int(a.get("maxArticlesPerRun", 32))]
    per = int(a.get("articlesPerCall", 8))
    if not todo or not ai.enabled:
        return 0
    if a.get("fetchFullText", True):
        with ThreadPoolExecutor(max_workers=6) as ex:
            texts = list(ex.map(lambda it: fetch_fulltext(it["link"]), todo))
        for it, t in zip(todo, texts):
            it["_full"] = t
    done = 0
    for i in range(0, len(todo), per):
        if not ai.enabled or ai.budget() <= 1:  # 1 Aufruf für den Morgenbericht freihalten
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


def ai_briefing(ai, items, markets, cfg):
    if not ai.enabled or ai.budget() <= 0:
        return None
    since = NOW - dt.timedelta(hours=26)
    recent = [x for x in items if parse_date(x["published"]) >= since] or items
    recent = sorted(recent, key=lambda x: -x["score"])
    per_cat = {}
    for x in recent:
        for c in x["cats"]:
            per_cat.setdefault(c, [])
            if len(per_cat[c]) < 8 and x not in per_cat[c]:
                per_cat[c].append(x)
    cats = {c["id"]: c["name"] for c in cfg["categories"]}
    blocks = ["MARKTDATEN:\n" + "\n".join(market_lines(markets))]
    for cid, its in per_cat.items():
        lines = []
        for x in its:
            summ = (x.get("ai") or {}).get("zusammenfassung") or x.get("teaser", "")
            lines.append(f"- id {x['id']} | {x['source']} | {x['title']} | {summ[:400]}")
        blocks.append(f"KATEGORIE {cid} ({cats.get(cid, cid)}):\n" + "\n".join(lines))
    res = parse_json_loose(ai.chat(SYS_BRIEFING, "\n\n".join(blocks), max_tokens=3000))
    if not isinstance(res, dict) or not res.get("ueberblick"):
        return None
    valid = {x["id"] for x in items}
    secs = []
    for s in res.get("abschnitte") or []:
        if isinstance(s, dict) and s.get("text"):
            secs.append({"kategorie": s.get("kategorie"), "text": str(s["text"])[:900],
                         "ids": [i for i in (s.get("ids") or []) if i in valid][:6]})
    return {"titel": str(res.get("titel", ""))[:160], "ueberblick": str(res["ueberblick"])[:2000],
            "maerkte": str(res.get("maerkte", ""))[:1500], "abschnitte": secs,
            "beobachten": [str(b)[:300] for b in (res.get("beobachten") or [])][:5],
            "art": "ki", "model": ai.model, "at": NOW.isoformat(timespec="seconds")}


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
            base = {k: p[k] for k in ("title", "link", "source", "published", "teaser", "lang", "feed") if k in p}
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
    markets = load_markets(cfg)

    # 3) KI
    ai = AI(cfg, state)
    n_ai = ai_articles(ai, items, cfg)
    log(f"KI-Auswertungen in diesem Lauf: {n_ai} (KI {'aktiv' if ai.enabled else 'aus: ' + str(ai.disabled_reason)})")
    for it in items:
        it["score"] = score(it)

    # 4) Morgenbericht – einmal täglich ab der eingestellten Stunde (deutsche Zeit) per KI,
    #    sonst automatisch zusammengestellt
    bnow = berlin_now()
    hour = int(cfg.get("ai", {}).get("briefingHourBerlin", 5))
    today = bnow.date().isoformat()
    briefing = None
    have_today = prev_brief and prev_brief.get("datum") == today and prev_brief.get("art") == "ki"
    force = os.environ.get("EXTRA_FORCE_BRIEFING") == "1"
    if (bnow.hour >= hour and not have_today) or force:
        briefing = ai_briefing(ai, items, markets, cfg)
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
        "ai": {"enabled": ai.enabled, "reason": ai.disabled_reason, "model": ai.model if ai.key else None,
               "callsToday": state.get("aiCalls", 0), "maxPerDay": ai.max_day},
    }
    def dump(name, obj):
        with open(os.path.join(args.out, name), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    dump("news.json", {**meta, "categories": cfg["categories"], "items": keep})
    dump("markets.json", {**meta, "markets": markets})
    dump("briefing.json", briefing)
    dump("state.json", {**state, "updated": meta["updated"]})
    dump("status.json", {**meta, "feeds": feed_status, "itemCount": len(keep),
                         "marketsOk": sum(1 for m in markets if m.get("ok")), "log": LOG[-80:]})
    log("Fertig.")


if __name__ == "__main__":
    sys.exit(main())
