#!/usr/bin/env python3
from __future__ import annotations
"""
Collect the next 7 days of movie showtimes around Seattle.

Sources:
  Fandango napi        – AMC / Regal / Cinemark / Landmark / indie houses listed on Fandango
  SIFF                 – Uptown, Egyptian, Film Center, Cinema Downtown
  Northwest Film Forum
  Grand Illusion Cinema
  The Beacon
  Central Cinema       – INDY Systems GraphQL

Each screening gets a language plus caption info:
  cap = "oc"        English open caption
        "en_subs"   non-English with English subtitles
        "en_subs?"  non-English, subtitles not stated (indie houses almost always subtitle)
        null        nothing special

Output:
  personal/movies/latest.json
  personal/movies/lang_cache.json   (Wikidata language lookups, reused across runs)
"""

import json
import os
import re
import sys
import time
import traceback
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

TZ = ZoneInfo("America/Los_Angeles")
DAYS = 7
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "personal", "movies")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

# Fandango is queried around several zip codes so the union covers
# Seattle, the Eastside, north and south King / Snohomish county.
FANDANGO_ZIPS = ["98101", "98103", "98004", "98052", "98036", "98188"]
FANDANGO_SKIP_CITIES = {"Bremerton", "Kingston", "Tacoma", "Monroe"}

AREAS = {
    "Seattle": "Seattle",
    "Shoreline": "North", "Lynnwood": "North", "Edmonds": "North",
    "Mountlake Terrace": "North", "Everett": "North", "Bothell": "North",
    "Bellevue": "Eastside", "Redmond": "Eastside", "Kirkland": "Eastside",
    "Issaquah": "Eastside", "Woodinville": "Eastside",
    "Renton": "South", "Tukwila": "South", "Kent": "South",
    "Auburn": "South", "Federal Way": "South",
    "Bainbridge Island": "Kitsap",
}
BIG_CHAINS = {"AMC", "Regal", "Cinemark Theatres", "Landmark", "Galaxy Theatres"}

LANGUAGES = [
    "English", "Spanish", "French", "German", "Italian", "Portuguese", "Russian",
    "Polish", "Czech", "Hungarian", "Romanian", "Greek", "Turkish", "Dutch",
    "Swedish", "Danish", "Norwegian", "Finnish", "Icelandic", "Ukrainian",
    "Serbian", "Croatian", "Georgian", "Armenian", "Persian", "Farsi", "Arabic",
    "Hebrew", "Hindi", "Tamil", "Telugu", "Malayalam", "Kannada", "Punjabi",
    "Bengali", "Marathi", "Gujarati", "Urdu", "Nepali", "Sinhala", "Mandarin",
    "Cantonese", "Chinese", "Japanese", "Korean", "Thai", "Vietnamese",
    "Tagalog", "Filipino", "Indonesian", "Malay", "Khmer", "Mongolian",
    "Tibetan", "Kazakh", "Amharic", "Swahili", "Yoruba", "Wolof", "Zulu",
    "Catalan", "Basque", "Galician", "Irish", "Welsh", "Latvian", "Lithuanian",
    "Estonian", "Slovak", "Slovenian", "Bulgarian", "Albanian", "Macedonian",
    "Bosnian", "Yiddish", "Lakota", "Navajo", "Inuktitut", "Maori", "Quechua",
    "Silent",
]
LANG_ALT = "|".join(LANGUAGES)

session = requests.Session()
session.headers["User-Agent"] = UA


def get(url, **kw):
    for attempt in range(3):
        try:
            r = session.get(url, timeout=30, **kw)
            r.raise_for_status()
            return r
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def soup(url):
    return BeautifulSoup(get(url).text, "html.parser")


def norm_title(t: str) -> str:
    t = unicodedata.normalize("NFKD", t.replace("\u2019", "'")).encode("ascii", "ignore").decode()
    t = t.lower().replace("'", "")
    t = re.sub(r"\((19|20)\d\d\)", "", t)
    t = re.sub(r"\b(on )?(16|35|70)mm\b|\b4k( restoration)?\b|\bopen capt\w*\b", "", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"^the ", "", t.strip())


def clean_title(t: str) -> str:
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s*\((19|20)\d\d\)$", "", t)
    if t.isupper() and len(t) > 3:
        t = t.title().replace("'S ", "'s ")
    return t


def fmt_tags(text: str) -> list[str]:
    tags = []
    for pat, tag in [(r"\b16mm\b", "16mm"), (r"\b35mm\b", "35mm"), (r"\b70mm\b", "70mm"),
                     (r"\bimax\b", "IMAX"), (r"\b3d\b", "3D"), (r"\bdolby\b", "Dolby"),
                     (r"\b4dx\b", "4DX"), (r"\bscreenx\b", "ScreenX")]:
        if re.search(pat, text, re.I):
            tags.append(tag)
    return tags


def lang_from_text(text: str) -> tuple[str | None, list[str], bool]:
    """Pull (language, subtitle languages, open caption) out of free text."""
    lang, subs, oc = None, [], False
    if re.search(r"open[- ]capt", text, re.I):
        oc = True
    m = re.search(rf"\b(?:in )?({LANG_ALT})(?: and ({LANG_ALT}))?(?: language)?,?\s+"
                  r"(?:with|w/)\s+(?:English|Eng\.?)\s+(?:subtitles|subs|intertitles)", text, re.I)
    if m:
        return m.group(1).title(), ["English"], oc
    m = re.search(rf"\bLanguages?:\s*((?:(?:{LANG_ALT})(?:,\s*|\s*/\s*|\s+and\s+)?)+)", text)
    if m:
        found = re.findall(LANG_ALT, m.group(1))
        lang = " / ".join(dict.fromkeys(found))
    m = re.search(rf"\b({LANG_ALT}) intertitles", text, re.I)
    if m and not lang:
        lang = "English" if m.group(1).lower() == "english" else m.group(1).title()
    if re.search(r"English subtitles|subtitled in English", text, re.I):
        subs = ["English"]
    if not lang:
        m = re.search(rf"\b(?:in|spoken in) ({LANG_ALT})\b", text)
        if m and m.group(1) != "English":
            lang = m.group(1)
    return lang, subs, oc


def cap_flag(lang, subs, oc, presume_subs=False):
    if lang and lang.split(" / ")[0] not in ("English", "Silent"):
        if "English" in subs or oc:
            return "en_subs"
        return "en_subs?" if presume_subs else None
    if oc:
        return "oc"
    return None


# ── Wikidata language fallback ─────────────────────────────────────────────────
FILM_CLASSES = {"Q11424", "Q24862", "Q506240", "Q202866", "Q93204", "Q226730",
                "Q24869", "Q17123180", "Q20650540", "Q229390", "Q29168811"}
_lang_cache: dict = {}
_lang_label: dict = {}


WIKIDATA_UA = "shanmy.github.io-movie-calendar/1.0 (https://shanmy.github.io)"


def wd_api(**params):
    time.sleep(0.5)
    r = requests.get("https://www.wikidata.org/w/api.php", timeout=30,
                     params={**params, "format": "json"}, headers={"User-Agent": WIKIDATA_UA})
    if r.status_code == 429:
        time.sleep(int(r.headers.get("Retry-After", 10)))
        return wd_api(**params)
    r.raise_for_status()
    return r.json()


def wikidata_lang(title: str, year: int | None) -> str | None:
    key = f"{norm_title(title)}|{year or ''}"
    if key in _lang_cache:
        return _lang_cache[key]
    result = None
    try:
        r = wd_api(action="wbsearchentities", search=clean_title(title), language="en",
                   type="item", limit=8)
        ids = [x["id"] for x in r.get("search", [])]
        if ids:
            ents = wd_api(action="wbgetentities", ids="|".join(ids), props="claims|labels",
                          languages="en")["entities"]
            for qid in ids:
                c = ents[qid].get("claims", {})
                p31 = {s["mainsnak"].get("datavalue", {}).get("value", {}).get("id") for s in c.get("P31", [])}
                if not p31 & FILM_CLASSES:
                    continue
                years = set()
                for s in c.get("P577", []):
                    v = s["mainsnak"].get("datavalue", {}).get("value", {}).get("time", "")
                    if v[1:5].isdigit():
                        years.add(int(v[1:5]))
                if year and years and not any(abs(y - year) <= 1 for y in years):
                    continue
                langs = [s["mainsnak"].get("datavalue", {}).get("value", {}).get("id")
                         for s in c.get("P364", [])]
                langs = [l for l in langs if l]
                if langs:
                    result = label_of(langs[0])
                break
    except Exception as e:  # network trouble shouldn't kill the run
        print(f"  wikidata lookup failed for {title!r}: {e}", file=sys.stderr)
        return None
    if result:
        result = {"Standard Mandarin": "Mandarin", "Modern Greek": "Greek",
                  "Yue Chinese": "Cantonese"}.get(result, result).replace(" language", "").title()
    _lang_cache[key] = result
    return result


def label_of(qid):
    if qid not in _lang_label:
        e = wd_api(action="wbgetentities", ids=qid, props="labels", languages="en")["entities"][qid]
        _lang_label[qid] = e.get("labels", {}).get("en", {}).get("value")
    return _lang_label[qid]


# ── Collected data ─────────────────────────────────────────────────────────────
theaters: dict[str, dict] = {}
films: dict[str, dict] = {}


def add_theater(tid, name, city, url, kind, address=""):
    theaters.setdefault(tid, {"name": name, "city": city, "area": AREAS.get(city, "Other"),
                              "url": url, "kind": kind, "address": address})


def film_entry(title, **meta):
    key = norm_title(title)
    f = films.setdefault(key, {"title": clean_title(title), "lang": None, "lang_src": None,
                               "year": None, "runtime": None, "url": None, "poster": None,
                               "screenings": []})
    for k, v in meta.items():
        if v and not f.get(k):
            f[k] = v
    return f


def tidy_tags(tags):
    tags = set(tags)
    if any(t.startswith("Dolby ") for t in tags):
        tags.discard("Dolby")
    if tags & {"IMAX", "Dolby Cinema", "Dolby", "4DX", "ScreenX", "XD", "RPX", "D-BOX"}:
        tags.discard("PLF")
    return sorted(tags)


SKIP_TITLES = re.compile(r"private (rental|event)|rent the beacon|^rental$", re.I)


def add_screening(film, theater_id, start: datetime, lang=None, subs=(), oc=False,
                  tags=(), url=None, note=None):
    if SKIP_TITLES.search(film["title"]):
        return
    film["screenings"].append({
        "th": theater_id,
        "start": start.astimezone(TZ).strftime("%Y-%m-%dT%H:%M"),
        "lang": lang, "subs": list(subs), "oc": bool(oc),
        "tags": tidy_tags(tags), "url": url, "note": note,
    })


# ── Fandango ───────────────────────────────────────────────────────────────────
def parse_fandango_amenities(names):
    lang, subs, oc = None, [], False
    for n in names:
        if n.lower() == "open caption":
            oc = True
        elif m := re.match(r"(.+?) with English Subtitles", n, re.I):
            lang, subs = m.group(1).strip().title(), subs + ["English"]
        elif m := re.match(r"(.+?) (?:Language|Dubbed|Dub)$", n, re.I):
            lang = m.group(1).strip().title()
        elif m := re.match(r"(.+?) Subtitles$", n, re.I):
            subs.append(m.group(1).strip().title())
    return lang, subs, oc


def fetch_fandango(days):
    def one(args):
        z, d = args
        out, page = [], 1
        while True:
            r = get("https://www.fandango.com/napi/theaterswithshowtimes", params={
                "zipCode": z, "city": "", "state": "", "date": d.isoformat(), "page": page,
                "favTheaterOnly": "false", "limit": 40, "isdesktop": "true",
                "filter": "open-theaters", "filterEnabled": "true"},
                headers={"Referer": f"https://www.fandango.com/{z}_movietimes"}).json()
            out += r.get("theaters", [])
            if page >= r.get("pagination", {}).get("totalPages", 1):
                return out
            page += 1

    jobs = [(z, d) for d in days for z in FANDANGO_ZIPS]
    seen = set()
    with ThreadPoolExecutor(4) as ex:
        for result in ex.map(one, jobs):
            for t in result:
                if t["city"] in FANDANGO_SKIP_CITIES:
                    continue
                tid = "fd-" + t["id"]
                add_theater(tid, t["name"], t["city"],
                            "https://www.fandango.com" + t.get("theaterPageUrl", ""),
                            "chain" if t.get("chainName") in BIG_CHAINS else "independent",
                            t.get("fullAddress", ""))
                for m in t.get("movies", []):
                    year = int(m["releaseDate"][:4]) if m.get("releaseDate") else None
                    f = film_entry(m["title"], year=year, runtime=m.get("runtime") or None,
                                   poster=m.get("poster", {}).get("size", {}).get("200"),
                                   url="https://www.fandango.com" + m["mopURI"] if m.get("mopURI") else None)
                    for v in m.get("variants", []):
                        header = v.get("filmFormatHeader", "")
                        for g in v.get("amenityGroups", []):
                            names = [a["name"] for a in g.get("amenities", [])]
                            lang, subs, oc = parse_fandango_amenities(names)
                            base_tags = fmt_tags(header + " " + " ".join(names))
                            if "Premium Format" in header:
                                base_tags.append("PLF")
                            for s in g.get("showtimes", []):
                                if not s.get("ticketingDate"):
                                    continue
                                k = (tid, m["title"], s.get("ticketingDate"), header, g.get("amenityString"))
                                if k in seen:
                                    continue
                                seen.add(k)
                                st = datetime.strptime(s["ticketingDate"], "%Y-%m-%d+%H:%M").replace(tzinfo=TZ)
                                tags = base_tags + [x["filterName"] for x in s.get("filmFormat", [])]
                                add_screening(f, tid, st, lang, subs, oc, tags)
    return len(seen)


# ── SIFF ───────────────────────────────────────────────────────────────────────
SIFF_VENUES = {
    "Uptown": ("SIFF Cinema Uptown", "511 Queen Anne Ave N"),
    "Egyptian": ("SIFF Cinema Egyptian", "805 E Pine St"),
    "Film Center": ("SIFF Film Center", "167 Republican St"),
    "Downtown": ("SIFF Cinema Downtown", "2100 4th Ave"),
}


def fetch_siff(days):
    details: dict[str, tuple] = {}
    n = 0
    for d in days:
        s = soup(f"https://www.siff.net/calendar?view=list&date={d.isoformat()}")
        for item in s.select("div.listing div.item"):
            a = item.select_one("h3 a")
            if not a:
                continue
            title, href = a.get_text(" ", strip=True), "https://www.siff.net" + a["href"]
            meta = item.select_one("p.meta")
            meta = meta.get_text(" ", strip=True) if meta else ""
            year = re.search(r"\b(19|20)\d\d\b", meta)
            runtime = re.search(r"(\d+)\s*min", meta)
            if href not in details:
                try:
                    txt = soup(href).get_text(" ", strip=True)
                except Exception:
                    txt = ""
                details[href] = lang_from_text(txt) + (
                    bool(re.search(r"Has Subtitles:\s*Yes", txt)),)
            lang, subs, oc, has_subs = details[href]
            if has_subs and lang and lang != "English" and "English" not in subs:
                subs = subs + ["English"]
            if re.search(r"open[- ]capt", title, re.I):
                oc = True
            f = film_entry(title, year=int(year.group()) if year else None,
                           runtime=int(runtime.group(1)) if runtime else None, url=href)
            for btn in item.select("a.elevent[data-screening]"):
                data = json.loads(btn["data-screening"])
                ms = int(re.search(r"\d+", data["Showtime"]).group())
                st = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
                venue = next((k for k in SIFF_VENUES if k in data.get("VenueName", "")), None)
                if not venue:
                    continue
                name, addr = SIFF_VENUES[venue]
                tid = "siff-" + venue.lower().replace(" ", "-")
                add_theater(tid, name, "Seattle", "https://www.siff.net/calendar", "independent", addr)
                add_screening(f, tid, st, lang, subs, oc, fmt_tags(title), href)
                n += 1
    return n


# ── Northwest Film Forum ───────────────────────────────────────────────────────
def fetch_nwff(days):
    tid = "nwff"
    add_theater(tid, "Northwest Film Forum", "Seattle", "https://nwfilmforum.org/calendar/",
                "independent", "1515 12th Ave")
    links = set()
    for start in (days[0], days[0] + timedelta(days=7)):
        s = soup(f"https://nwfilmforum.org/calendar/?start={start.isoformat()}")
        for it in s.select("div.calendar__item--film a.calendar__item__link"):
            links.add(it["href"])
    lo, hi = days[0], days[-1]
    n = 0
    for href in sorted(links):
        s = soup(href)
        title = s.select_one("h1")
        title = title.get_text(" ", strip=True) if title else href.rstrip("/").split("/")[-1]
        txt = s.get_text(" ", strip=True)
        lang, subs, oc = lang_from_text(txt)
        about = re.search(r"\(([^()]*\b(?:19|20)\d\d\b[^()]*)\)", txt)
        year = re.search(r"\b(19|20)\d\d\b", about.group(1)) if about else None
        rt = re.search(r"(\d+)\s*min", about.group(1)) if about else None
        f = film_entry(title, year=int(year.group()) if year else None,
                       runtime=int(rt.group(1)) if rt else None, url=href)
        for t in {t["datetime"] for t in s.select("time[datetime]")}:
            st = datetime.strptime(t[:16], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            if lo <= st.date() <= hi:
                add_screening(f, tid, st, lang, subs, oc, fmt_tags(title), href)
                n += 1
    return n


# ── Grand Illusion ─────────────────────────────────────────────────────────────
def fetch_grand_illusion(days):
    tid = "grand-illusion"
    add_theater(tid, "Grand Illusion Cinema", "Seattle", "https://grandillusioncinema.org/",
                "independent", "")
    s = soup("https://grandillusioncinema.org/")
    lo, hi = days[0], days[-1]
    n = 0
    for card in s.select("div.film-card"):
        a = card.select_one("a.film-title")
        if not a:
            continue
        title, href = a.get_text(" ", strip=True), a["href"]
        info = card.select_one(".film-card--film-info")
        info = info.get_text(" ", strip=True) if info else ""
        desc = card.get_text(" ", strip=True)
        lang, subs, oc = lang_from_text(desc)
        year = re.search(r"\b(19|20)\d\d\b", info)
        rt = re.search(r"(\d+)\s*min", info)
        loc = re.search(r"Screening location:\s*([^–(]+)", desc)
        f = film_entry(title, year=int(year.group()) if year else None,
                       runtime=int(rt.group(1)) if rt else None, url=href)
        for li in card.select("li.screening"):
            try:
                st = datetime.strptime(li.get_text(strip=True), "%A, %b %d, %Y, %I:%M%p").replace(tzinfo=TZ)
            except ValueError:
                continue
            if lo <= st.date() <= hi:
                add_screening(f, tid, st, lang, subs, oc, fmt_tags(title + " " + info), href,
                              note=f"at {loc.group(1).strip()}" if loc else None)
                n += 1
    return n


# ── The Beacon ─────────────────────────────────────────────────────────────────
def fetch_beacon(days):
    tid = "beacon"
    add_theater(tid, "The Beacon", "Seattle", "https://thebeacon.film/calendar",
                "independent", "4405 Rainier Ave S")
    s = soup("https://thebeacon.film/calendar")
    lo, hi = days[0], days[-1]
    details = {}
    n = 0
    for day in s.select("div.cal-list-day"):
        dtxt = day.select_one(".cal-list-date").get_text(strip=True)
        dd = datetime.strptime(f"{dtxt} {lo.year}", "%A, %B %d %Y").date()
        if dd < lo - timedelta(days=60):
            dd = dd.replace(year=lo.year + 1)
        if not lo <= dd <= hi:
            continue
        for e in day.select("div.cal-list-entry"):
            a = e.select_one("a.cal-list-movie")
            if not a or e.select_one(".sold-out-btn") and "rent the beacon" in a.get_text().lower():
                continue
            href = "https://thebeacon.film" + a["href"]
            title = a.get_text(" ", strip=True)
            if href not in details:
                try:
                    txt = soup(href).get_text(" ", strip=True)
                except Exception:
                    txt = ""
                yr = re.search(re.escape(title) + r"\s+((?:19|20)\d\d)\b", txt)
                rt = re.search(r"Runtime\s+(\d+)", txt)
                details[href] = (lang_from_text(txt), int(yr.group(1)) if yr else None,
                                 int(rt.group(1)) if rt else None)
            (lang, subs, oc), year, runtime = details[href]
            tm = e.select_one(".cal-list-time").get_text(strip=True)
            st = datetime.strptime(f"{dd} {tm}", "%Y-%m-%d %I:%M %p").replace(tzinfo=TZ)
            f = film_entry(title, year=year, runtime=runtime, url=href)
            add_screening(f, tid, st, lang, subs, oc, fmt_tags(title), href)
            n += 1
    return n


# ── Central Cinema (INDY Systems GraphQL) ──────────────────────────────────────
def fetch_central(days):
    tid = "central"
    add_theater(tid, "Central Cinema", "Seattle", "https://central-cinema.com/",
                "independent", "1411 21st Ave")
    q = ("query($d: String) { showingsForDate(date: $d) { data { id time "
         "showingBadges { displayName } movie { name urlSlug synopsis } } } }")
    n = 0
    for d in days:
        r = session.post("https://central-cinema.com/graphql", timeout=30,
                         json={"query": q, "variables": {"d": d.isoformat()}},
                         headers={"client-type": "consumer", "site-id": "275"})
        r.raise_for_status()
        for sh in r.json()["data"]["showingsForDate"]["data"]:
            m = sh["movie"]
            badges = " ".join(b["displayName"] for b in sh.get("showingBadges") or [])
            text = BeautifulSoup(m.get("synopsis") or "", "html.parser").get_text(" ")
            lang, subs, oc = lang_from_text(badges + " " + text)
            st = datetime.fromisoformat(sh["time"].replace("Z", "+00:00"))
            if st.astimezone(TZ).date() != d:
                continue
            url = f"https://central-cinema.com/movies/{m['urlSlug']}" if m.get("urlSlug") else None
            f = film_entry(m["name"], url=url)
            add_screening(f, tid, st, lang, subs, oc, fmt_tags(m["name"] + " " + badges), url)
            n += 1
    return n


SOURCES = [
    ("Fandango (chains & listed indies)", fetch_fandango),
    ("SIFF", fetch_siff),
    ("Northwest Film Forum", fetch_nwff),
    ("Grand Illusion", fetch_grand_illusion),
    ("The Beacon", fetch_beacon),
    ("Central Cinema", fetch_central),
]


def resolve_languages():
    """Give every film a language; fill each screening's caption flag."""
    for f in films.values():
        explicit = [s["lang"] for s in f["screenings"] if s["lang"]]
        if explicit:
            f["lang"], f["lang_src"] = max(set(explicit), key=explicit.count), "listing"
        indie_only = all(not s["th"].startswith("fd-") or theaters[s["th"]]["kind"] == "independent"
                         for s in f["screenings"])
        # Chains tag foreign-language prints explicitly; an untagged chain film is
        # English (often a dub), so only ask Wikidata for indie films or subbed ones.
        if not f["lang"] and (indie_only or any(s["subs"] for s in f["screenings"])):
            wd = wikidata_lang(f["title"], f["year"])
            if wd:
                f["lang"], f["lang_src"] = wd, "wikidata"
        if not f["lang"] and (not indie_only or (f["year"] and f["year"] >= date.today().year - 1)):
            f["lang"], f["lang_src"] = "English", "assumed"
        for s in f["screenings"]:
            lang = s["lang"] or f["lang"]
            s["lang"] = lang
            presume = not s["th"].startswith("fd-") or theaters[s["th"]]["kind"] == "independent"
            s["cap"] = cap_flag(lang, s["subs"], s["oc"], presume_subs=presume)


def main():
    global _lang_cache
    os.makedirs(OUT_DIR, exist_ok=True)
    cache_path = os.path.join(OUT_DIR, "lang_cache.json")
    if os.path.exists(cache_path):
        _lang_cache = json.load(open(cache_path))

    today = datetime.now(TZ).date()
    days = [today + timedelta(days=i) for i in range(DAYS)]
    status = {}
    for name, fn in SOURCES:
        t0 = time.time()
        try:
            count = fn(days)
            status[name] = {"ok": True, "count": count}
            print(f"{name}: {count} screenings ({time.time() - t0:.0f}s)")
        except Exception as e:
            traceback.print_exc()
            status[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
            print(f"{name}: FAILED {e}", file=sys.stderr)

    resolve_languages()

    used = {s["th"] for f in films.values() for s in f["screenings"]}
    out_films = []
    for f in sorted(films.values(), key=lambda f: (-len(f["screenings"]), f["title"])):
        if not f["screenings"]:
            continue
        f["screenings"].sort(key=lambda s: (s["start"], s["th"]))
        for s in f["screenings"]:
            for k in ("subs", "tags"):
                if not s[k]:
                    del s[k]
            for k in ("url", "note", "cap"):
                if s.get(k) is None:
                    s.pop(k, None)
            s.pop("oc")
        out_films.append(f)

    out = {
        "generated_at": datetime.now(TZ).isoformat(timespec="minutes"),
        "days": [d.isoformat() for d in days],
        "sources": status,
        "theaters": {k: v for k, v in theaters.items() if k in used},
        "films": out_films,
    }
    with open(os.path.join(OUT_DIR, "latest.json"), "w") as fp:
        json.dump(out, fp, ensure_ascii=False, separators=(",", ":"))
    with open(cache_path, "w") as fp:
        json.dump(_lang_cache, fp, ensure_ascii=False, indent=0, sort_keys=True)
    total = sum(len(f["screenings"]) for f in out_films)
    print(f"Wrote {len(out_films)} films, {total} screenings, {len(used)} theaters")
    if not any(s["ok"] for s in status.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
