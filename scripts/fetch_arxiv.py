#!/usr/bin/env python3
from __future__ import annotations
"""
Fetch and classify cs.CV papers from arxiv recent listings.
For 'interested' papers, optionally call Gemini API for structured analysis.

Output:
  personal/arxiv/YYYY-MM-DD.json
  personal/arxiv/latest.json
"""

import argparse
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

ARXIV_BASE = "https://arxiv.org/list/cs.CV"
ARXIV_URL  = f"{ARXIV_BASE}/recent"

# ════════════════════════════════════════════════════════════════════════════════
# TITLE BLACKLIST
# If any of these strings appear in a paper's TITLE (case-insensitive),
# it is immediately labelled "not_for_me" before any other rule runs.
# Edit this list freely — one string per line is easiest to maintain.
# ════════════════════════════════════════════════════════════════════════════════
TITLE_BLACKLIST: list[str] = [
    # 3-D / geometry
    "3d gaussian", "gaussian splatting", "nerf", "neural radiance",
    "point cloud", "3d reconstruction", "depth estimation",
    "mesh generation", "surface reconstruction",
    # Medical / biology
    "medical", "clinical", "pathology", "radiology",
    "ct scan", "mri", "ultrasound", "histology", "surgical", "biometric"
    # Autonomous systems / remote sensing
    "autonomous driving", "self-driving", "lidar",
    "satellite", "remote sensing", "traffic"
    # Other downstream
    "weather", "document", "scene text", "deepfake", "watermark", "attack", "uav", "safety",
    "ocr", "pedestrian", "biometrics", "anomaly"
]

# ── Classification rules ──────────────────────────────────────────────────────

INTERESTED = {
    "image_video_generation": [
        "diffusion", "flow matching", "consistency model",
        "image generation", "video generation", "image synthesis", "video synthesis",
        "text-to-image", "text-to-video", "t2i", "t2v",
        "image editing", "video editing", "inpainting", "outpainting",
        "image-to-video", "video-to-video", "style transfer",
        "generative model", "latent diffusion", "score matching",
        "rectified flow", "stable diffusion",
    ],
    "multimodal_llm": [
        "multimodal", "vision-language", "vision language",
        "vlm", "mllm", "large language model", "visual language model",
        "visual question answering", "vqa", "image captioning",
        "visual instruction", "visual chat", "gpt-4v", "llava", "qwen-vl",
        "internvl", "gemini", "visual grounding",
        "referring expression", "visual reasoning",
    ],
    "human_generation": [
        "talking head", "facial animation", "face reenactment",
        "human motion synthesis", "motion generation", "dance generation",
        "gesture generation", "human video generation", "human synthesis",
        "portrait generation", "face generation", "face synthesis",
        "human avatar", "digital human", "virtual human",
        "body generation", "human image generation",
    ],
}

NOT_INTERESTED = {
    "medical": [
        "medical image", "clinical", "pathology", "radiology",
        "mri", "ct scan", "x-ray", "cancer", "tumor", "lesion",
        "disease", "patient", "hospital", "therapeutic", "diagnosis",
        "histology", "endoscopy", "ultrasound", "dermoscopy",
        "fundus", "retinal", "microscopy", "cytology",
    ],
    "3d": [
        "point cloud", "nerf", "neural radiance field",
        "3d reconstruction", "lidar", "voxel", "mesh reconstruction",
        "depth estimation", "surface reconstruction",
        "multi-view stereo", "structure from motion", "sfm",
        "novel view synthesis", "3d gaussian", "gaussian splatting", "3d scene",
    ],
    "downstream_tasks": [
        "autonomous driving", "traffic monitoring", "satellite imagery",
        "remote sensing", "retail analytics", "surveillance",
        "crowd counting", "weather forecasting", "crop disease",
        "agriculture", "document understanding", "scene text recognition",
        "license plate", "industrial inspection", "defect detection",
    ],
    "pure_visual_understanding": [
        "object detection", "image classification",
        "semantic segmentation", "instance segmentation",
        "action recognition", "optical flow estimation",
        "visual tracking", "salient object detection",
        "re-identification", "person re-id",
    ],
}


def classify(title: str, abstract: str) -> dict:
    # ── Title blacklist takes priority over everything ────────────────────────
    title_lower = title.lower()
    for kw in TITLE_BLACKLIST:
        if kw.lower() in title_lower:
            return {"label": "not_for_me", "auto_tags": ["title_blacklist"],
                    "blacklist_match": kw}

    text = (title + " " + abstract).lower()

    not_tags = []
    for reason, keywords in NOT_INTERESTED.items():
        if any(kw in text for kw in keywords):
            not_tags.append(reason)

    int_tags = []
    for category, keywords in INTERESTED.items():
        if any(kw in text for kw in keywords):
            int_tags.append(category)

    if int_tags:
        if "medical" in not_tags:
            return {"label": "not_for_me", "auto_tags": not_tags}
        return {
            "label": "interested",
            "auto_tags": int_tags,
            "caution_tags": [t for t in not_tags if t != "medical"],
        }

    if not_tags:
        return {"label": "not_for_me", "auto_tags": not_tags}

    return {"label": "neutral", "auto_tags": []}


# ── Scraper ───────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (academic research reader)"
    )
}


def _get_soup(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def _detect_total_and_date(soup: BeautifulSoup) -> tuple[int, str]:
    """
    Parse the h3 header that arxiv renders as e.g.:
      "Wed, 4 Mar 2026 (showing first 50 of 136 entries )"
    Returns (total_count, date_string).
    """
    for h3 in soup.find_all("h3"):
        text = h3.get_text(" ", strip=True)
        m = re.search(r"of\s+(\d+)\s+entr", text)
        if m:
            total = int(m.group(1))
            date_str = re.sub(r"\(.*\)", "", text).strip()
            return total, date_str
    # Fallback: no pagination info found (all entries fit on one page)
    return 0, ""


def _parse_dl(dl) -> list[dict]:
    """Parse all papers from a <dl> element."""
    papers = []
    for dt, dd in zip(dl.find_all("dt"), dl.find_all("dd")):
        link_tag = dt.find("a", href=re.compile(r"^/abs/"))
        if not link_tag:
            continue

        arxiv_id = link_tag.get_text(strip=True)
        link = "https://arxiv.org" + link_tag["href"]
        is_cross = bool(re.search(r"cross.list", dt.get_text(), re.I))

        title_div = dd.find("div", class_="list-title")
        if not title_div:
            continue
        title = re.sub(r"^Title:\s*", "", title_div.get_text(strip=True)).strip()

        authors_div = dd.find("div", class_="list-authors")
        authors = ([a.get_text(strip=True) for a in authors_div.find_all("a")]
                   if authors_div else [])

        abstract_p = dd.find("p", class_="mathjax")
        abstract = abstract_p.get_text(" ", strip=True) if abstract_p else ""

        papers.append({
            "id": arxiv_id,
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "link": link,
            "cross_listed": is_cross,
            "classification": classify(title, abstract),
            "analysis": None,
        })

    return papers


def _parse_papers(soup: BeautifulSoup) -> list[dict]:
    """Extract all papers from a single page (uses the first <dl id='articles'>)."""
    dl = soup.find("dl", id="articles")
    return _parse_dl(dl) if dl else []


def _collect_papers_for_date(date_str: str) -> tuple[list[dict], str]:
    """
    Scrape the arxiv monthly listing (YYMM URL) and return only papers
    announced on date_str (YYYY-MM-DD).

    arxiv monthly listings are reverse-chronological and paginate at 50 entries
    across the whole month, so we scan pages until we've passed the target date.
    """
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    monthly_url = f"{ARXIV_BASE}/{dt.strftime('%y%m')}"

    # h3 text example: "Mon, 3 Mar 2026  (showing first 50 of 136 entries )"
    day_pat = re.compile(
        rf"\b{dt.day}\b\s+{dt.strftime('%b')}\s+{dt.year}\b", re.I
    )

    seen: dict[str, dict] = {}   # id → paper (dedup)
    arxiv_date = ""
    expected_total: int | None = None
    found_target = False
    skip = 0
    page = 1

    print(f"  Monthly listing: {monthly_url}", file=sys.stderr)

    while True:
        url = f"{monthly_url}?skip={skip}&show=50" if skip else monthly_url
        print(f"  Fetching page {page} (skip={skip}) …", file=sys.stderr)
        soup = _get_soup(url)

        h3s = soup.find_all("h3")
        target_h3 = next((h for h in h3s if day_pat.search(h.get_text())), None)

        if target_h3:
            found_target = True
            if not arxiv_date:
                h3_text = target_h3.get_text(" ", strip=True)
                m = re.search(r"of\s+(\d+)\s+entr", h3_text)
                expected_total = int(m.group(1)) if m else None
                arxiv_date = re.sub(r"\(.*?\)", "", h3_text).strip()

            # Collect papers between this h3 and the next h3 (next date's section)
            for sibling in target_h3.find_next_siblings():
                if sibling.name == "h3":
                    break
                if sibling.name == "dl":
                    for p in _parse_dl(sibling):
                        seen.setdefault(p["id"], p)

        elif found_target:
            # We've already seen the target date's h3 on a previous page;
            # this page has a different h3 (earlier date) — we're done.
            if h3s:
                break
            # No h3 at all → continuation of target date across page boundary
            dl = soup.find("dl", id="articles")
            if dl:
                for p in _parse_dl(dl):
                    seen.setdefault(p["id"], p)
            else:
                break

        # Stop early if we have all expected papers
        if expected_total and len(seen) >= expected_total:
            break

        # Stop if the page has no articles at all (end of listing)
        if not soup.find("dl", id="articles") and not target_h3:
            break

        # Safety: don't scan more than 200 pages (~10 000 papers into the month)
        if page >= 200:
            print("  Safety page limit reached.", file=sys.stderr)
            break

        skip += 50
        page += 1
        time.sleep(1)

    if not seen:
        print(f"  No papers found for {date_str} in {monthly_url}.", file=sys.stderr)
        return [], ""

    print(f"  Collected {len(seen)} papers for {arxiv_date}", file=sys.stderr)
    return list(seen.values()), arxiv_date


_ATOM_NS   = {"atom": "http://www.w3.org/2005/Atom"}
_ARXIV_AFF = "{http://arxiv.org/schemas/atom}affiliation"


def _fetch_abstracts(
    raw_ids: list[str],
    cache_path: str | None = None,
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """
    Batch-fetch abstracts and per-author affiliations from the arxiv API.
    raw_ids: list of IDs as they appear on the listing page,
             e.g. "arXiv:2503.01234" or "arXiv:2503.01234 [cs.CV]"
    Returns ({clean_id: abstract_text}, {clean_id: [affiliation_per_author]}).
    Affiliations are empty strings where the author didn't submit one.

    If cache_path is given, partial results are saved there after each batch
    so a crashed run can resume without re-fetching completed batches.
    """
    # Strip prefix and version suffix: "arXiv:2503.01234v2 [cs.CV]" → "2503.01234"
    clean = [re.sub(r"v\d+$", "", re.sub(r"^arXiv:", "", i.split()[0]))
             for i in raw_ids]

    abstracts:    dict[str, str]       = {}
    affiliations: dict[str, list[str]] = {}

    # Load any previously saved partial results
    if cache_path and os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as _f:
            _cache = json.load(_f)
        abstracts    = _cache.get("abstracts", {})
        affiliations = _cache.get("affiliations", {})
        already = sum(1 for c in clean if c in abstracts)
        if already:
            print(f"  Resuming from cache: {already}/{len(clean)} abstracts already fetched …",
                  file=sys.stderr)
    BATCH = 80          # arxiv API is comfortable with ~100 IDs at once
    API   = "http://export.arxiv.org/api/query"

    for start in range(0, len(clean), BATCH):
        batch = [c for c in clean[start:start + BATCH] if c not in abstracts]
        if not batch:
            print(f"  API: abstracts {start+1}–{start+BATCH} already cached, skipping …",
                  file=sys.stderr)
            continue
        print(f"  API: fetching abstracts {start+1}–{start+len(batch)} …",
              file=sys.stderr)

        delay = 15
        resp = None
        for _ in range(6):
            time.sleep(delay)
            try:
                resp = requests.get(
                    API,
                    params={"id_list": ",".join(batch), "max_results": len(batch)},
                    headers=HEADERS,
                    timeout=90,
                )
            except (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError) as exc:
                print(f"  Network error ({exc.__class__.__name__}); waiting {delay}s before retry …",
                      file=sys.stderr)
                delay = min(delay * 2, 120)
                continue
            if resp.status_code in (429, 503):
                print(f"  HTTP {resp.status_code}; waiting {delay}s before retry …",
                      file=sys.stderr)
                delay = min(delay * 2, 120)
                continue
            resp.raise_for_status()
            break
        else:
            if resp is not None:
                resp.raise_for_status()
            raise RuntimeError("All retries exhausted for abstract batch")

        root = ET.fromstring(resp.content)
        for entry in root.findall("atom:entry", _ATOM_NS):
            id_el  = entry.find("atom:id",      _ATOM_NS)
            sum_el = entry.find("atom:summary", _ATOM_NS)
            if id_el is None or sum_el is None:
                continue
            # id URL → clean ID, e.g. http://arxiv.org/abs/2503.01234v1
            m = re.search(r"/abs/(.+?)v?\d*$", id_el.text.strip())
            if not m:
                continue
            cid = m.group(1)
            abstracts[cid] = " ".join(sum_el.text.split())

            # Extract per-author affiliations (optional submission field)
            entry_affs = []
            for author_el in entry.findall("atom:author", _ATOM_NS):
                aff_el = author_el.find(_ARXIV_AFF)
                entry_affs.append(aff_el.text.strip() if aff_el is not None else "")
            affiliations[cid] = entry_affs

        if cache_path:
            with open(cache_path, "w", encoding="utf-8") as _f:
                json.dump({"abstracts": abstracts, "affiliations": affiliations}, _f)

    return abstracts, affiliations


def _fetch_affiliations_openalex(
    papers: list[dict],
    email: str = "arxiv-reader@example.com",
) -> dict[str, list[str]]:
    """
    Supplement affiliations via the OpenAlex API for papers that still have
    all-empty affiliation lists after the arxiv XML pass.
    Returns {clean_id: [affiliation_per_author]} for papers that OpenAlex knows about.
    Requires no API key; polite-use email is sent in the mailto param.
    """
    # Only query papers with no affiliations yet
    missing = [
        p for p in papers
        if not any(p.get("author_affiliations", []))
    ]
    if not missing:
        return {}

    clean_ids = [
        re.sub(r"v\d+$", "", re.sub(r"^arXiv:", "", p["id"].split()[0]))
        for p in missing
    ]
    id_to_paper = {
        re.sub(r"v\d+$", "", re.sub(r"^arXiv:", "", p["id"].split()[0])): p
        for p in missing
    }

    result: dict[str, list[str]] = {}
    BATCH = 50
    API   = "https://api.openalex.org/works"

    for start in range(0, len(clean_ids), BATCH):
        batch = clean_ids[start:start + BATCH]
        filter_str = "|".join(f"arxiv:{cid}" for cid in batch)
        print(f"  OpenAlex: fetching affiliations {start+1}–{start+len(batch)} …",
              file=sys.stderr)
        try:
            resp = requests.get(
                API,
                params={
                    "filter":  f"ids.arxiv:{filter_str}",
                    "select":  "ids,authorships",
                    "per-page": BATCH,
                    "mailto":  email,
                },
                headers=HEADERS,
                timeout=30,
            )
            resp.raise_for_status()
            works = resp.json().get("results", [])
        except Exception as e:
            print(f"  OpenAlex error: {e}", file=sys.stderr)
            continue

        for work in works:
            arxiv_id = (work.get("ids") or {}).get("arxiv", "")
            m = re.search(r"arxiv\.org/abs/(.+?)v?\d*$", arxiv_id)
            if not m:
                continue
            cid = m.group(1)
            paper = id_to_paper.get(cid)
            if not paper:
                continue

            authorships = work.get("authorships", [])
            # Build affiliation list aligned to author order in our paper data
            # OpenAlex author order may differ; match by name where possible
            name_to_aff: dict[str, str] = {}
            for aship in authorships:
                raw_affs = aship.get("raw_affiliation_strings") or []
                author_name = (aship.get("author") or {}).get("display_name", "")
                if author_name and raw_affs:
                    name_to_aff[author_name.lower()] = raw_affs[0]

            affs = []
            for name in paper.get("authors", []):
                affs.append(name_to_aff.get(name.lower(), ""))
            result[cid] = affs

        if start + BATCH < len(clean_ids):
            time.sleep(1)

    return result


def fetch_papers(base_url: str = ARXIV_URL, date_str: str | None = None) -> tuple[list[dict], str]:
    if date_str:
        # ── Historical date: scrape monthly listing ───────────────────────────
        papers, arxiv_date = _collect_papers_for_date(date_str)
    else:
        # ── Step 1: scrape recent listing for IDs / titles / authors ─────────
        print("  Fetching page 1 …", file=sys.stderr)
        soup = _get_soup(base_url)

        total, arxiv_date = _detect_total_and_date(soup)
        papers = _parse_papers(soup)
        print(f"  Page 1: {len(papers)} papers  (total announced: {total or '?'})",
              file=sys.stderr)

        # ── Step 2: paginate remaining pages ──────────────────────────────────
        if total > len(papers):
            seen_ids = {p["id"] for p in papers}
            skip = len(papers)
            page = 2
            while skip < total:
                print(f"  Fetching page {page} (skip={skip}) …", file=sys.stderr)
                page_soup = _get_soup(f"{base_url}?skip={skip}&show=50")
                new = [p for p in _parse_papers(page_soup) if p["id"] not in seen_ids]
                if not new:
                    break
                papers.extend(new)
                seen_ids.update(p["id"] for p in new)
                skip += 50
                page += 1
            print(f"  Total collected: {len(papers)}", file=sys.stderr)

    if not papers:
        return [], arxiv_date

    # ── Fetch abstracts + affiliations from arxiv API ────────────────────────
    cache_file = f"personal/arxiv/.abstract_cache_{date_str or 'latest'}.json"
    abstracts, affiliations = _fetch_abstracts([p["id"] for p in papers],
                                               cache_path=cache_file)
    if os.path.exists(cache_file):
        os.remove(cache_file)
    for p in papers:
        clean_id = re.sub(r"v\d+$", "", re.sub(r"^arXiv:", "", p["id"].split()[0]))
        p["abstract"] = abstracts.get(clean_id, "")
        p["author_affiliations"] = affiliations.get(clean_id, [])

    # ── Re-classify now that we have full abstracts ───────────────────────────
    for p in papers:
        p["classification"] = classify(p["title"], p["abstract"])

    return papers, arxiv_date


# ── Gemini analysis ───────────────────────────────────────────────────────────

PAPER_SYSTEM_PROMPT = """为了更好地理解学术论文，并从中提取有价值的信息，一个结构化的阅读和整理框架至关重要。你需要构建一个体系，帮助你系统地拆解论文，抓住核心要点，并建立知识关联。以下是一个更详细的结构化框架，以及关于信息量和需要仔细阅读的部分的建议：


* **论文基本信息 (Bibliographic Information):**
* **标题 (Title):** 论文的核心主题是什么？
* **作者 (Authors):** 作者是谁？他们的研究背景和机构是什么？
* **发表期刊/会议 (Journal/Conference):** 期刊/会议的领域和声誉如何？（影响论文的影响力和可信度）
* **发表年份 (Publication Year)
* **摘要 (Abstract):** 论文的目的是什么？主要方法和结果是什么？结论是什么？（快速了解论文的核心内容）

* **整体概括 :**
* **研究背景与动机 (Background & Motivation - Why):** 论文研究的问题是什么？为什么这个问题重要？该领域之前的研究状况如何？论文的创新点或研究空白是什么？
* **核心贡献/主要发现 (Main Contribution/Findings - What):** 论文的主要贡献是什么？得出了哪些重要的结论或发现？解决了什么问题？

* **方法 (Methods - 核心技术与实现细节):**
核心要点：对于论文中的方法解释的同时应结合原文内容如数学公式或专业词汇，以帮助读者更好地理解原论文的方法。
* **方法原理 (Methodology Principles):** 所用方法的基本原理是什么？核心思想是什么？
* **方法步骤与流程 (Steps & Procedures):** 方法的具体步骤和流程是什么？输入的数据是怎么获取并处理的？如何一步步实现研究目标？


* **实验结果 (Results - 数据解读与结果分析):**
解读实验结果时不应该只是阐述数据，而是应该基于数据集的特点去分析内容，思考能给到我们什么启发

**信息量控制的原则：**
* **抓住主次：** 论文的信息有主次之分，核心观点和关键细节是主要的，一些辅助性的描述可以适当忽略。




**总结：**

结构化的阅读框架和适量的信息深度，是理解学术论文的关键。通过宏观和微观结合的框架，逐步深入论文的各个部分，抓住核心观点和关键细节。记住，阅读学术论文是一个主动思考和批判性分析的过程，不仅仅是被动地接受信息。

**补充：**
1. 注意数学公式和符号都要使用 LaTeX 定界符" $ "，并且行内公式 (Inline formulas): 将 LaTeX 代码包裹在单个美元符号 `$...$` 中。这会使公式嵌入到文本行内。块级/独立公式 (Display formulas): 将 LaTeX 代码包裹在双美元符号 `$$...$$` 中。这会使公式单独成行并居中显示。
2. 我认为论文中每一张图片的添加都有着一定的价值，因此在回复中第一次引用一张图片时，需要对图片进行简要的解释说明，以帮助读者直观地理解这张图片（只需要利用文本进行解释即可，不需要列出这张图片）。

注意：本次分析仅基于论文标题和摘要，因此方法和实验部分的分析会有所限制，请以摘要中提及的内容为准。"""


def analyze_paper_with_gemini(paper: dict, api_key: str) -> str | None:
    """Call Gemini API to generate structured analysis for one paper."""
    authors_str = ", ".join(paper["authors"][:5])
    if len(paper["authors"]) > 5:
        authors_str += f" et al. ({len(paper['authors'])} authors)"

    user_message = (
        f"请分析以下论文（仅提供标题和摘要）：\n\n"
        f"**标题:** {paper['title']}\n"
        f"**作者:** {authors_str}\n"
        f"**arxiv ID:** {paper['id']}\n\n"
        f"**摘要:**\n{paper['abstract']}"
    )

    payload = {
        "system_instruction": {"parts": [{"text": PAPER_SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": user_message}], "role": "user"}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 2048,
        },
    }

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={api_key}"
    )

    try:
        resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        print(f"  Gemini error for {paper['id']}: {e}", file=sys.stderr)
        return None


def analyze_interested_papers(papers: list[dict], api_key: str) -> None:
    """In-place: add Gemini analysis to all interested papers."""
    targets = [p for p in papers if p["classification"]["label"] == "interested"]
    print(f"Analyzing {len(targets)} interested papers with Gemini …", file=sys.stderr)

    for i, paper in enumerate(targets, 1):
        print(f"  [{i}/{len(targets)}] {paper['id']} – {paper['title'][:60]}…",
              file=sys.stderr)
        paper["analysis"] = analyze_paper_with_gemini(paper, api_key)
        # Be polite to the API – 1 request/second is fine for small batches
        if i < len(targets):
            time.sleep(1)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fetch cs.CV papers from arxiv.")
    parser.add_argument(
        "--date", metavar="YYYY-MM-DD",
        help="Fetch papers for a specific date instead of today's recent listing.",
    )
    parser.add_argument(
        "--affiliations", action="store_true",
        help="Supplement missing affiliations via the OpenAlex API (free, no key needed).",
    )
    args = parser.parse_args()

    if args.date:
        try:
            datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            print(f"Invalid date '{args.date}'. Use YYYY-MM-DD format.", file=sys.stderr)
            sys.exit(1)
        print(f"Fetching papers for {args.date} …", file=sys.stderr)
        papers, arxiv_date = fetch_papers(date_str=args.date)
    else:
        print("Fetching papers from arxiv cs.CV/recent …", file=sys.stderr)
        papers, arxiv_date = fetch_papers()

    if not papers:
        print("No papers found – aborting.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(papers)} papers  (header: {arxiv_date!r})", file=sys.stderr)

    # ── Optionally supplement affiliations via OpenAlex ───────────────────────
    if args.affiliations:
        openalex_affs = _fetch_affiliations_openalex(papers)
        applied = 0
        for p in papers:
            clean_id = re.sub(r"v\d+$", "", re.sub(r"^arXiv:", "", p["id"].split()[0]))
            if clean_id in openalex_affs:
                p["author_affiliations"] = openalex_affs[clean_id]
                applied += 1
        print(f"  OpenAlex: filled affiliations for {applied} papers", file=sys.stderr)

    today = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir = "personal/arxiv"
    os.makedirs(out_dir, exist_ok=True)

    # ── Bake user annotations into paper JSON ─────────────────────────────────
    # Reads user_annotations.json (exported from the browser and committed to repo).
    # For each paper whose ID appears in the user's overrides, adds a
    # "user_override" field so fresh browsers display the correct label
    # before the user interacts with the page.
    annotations_path = f"{out_dir}/user_annotations.json"
    if os.path.exists(annotations_path):
        with open(annotations_path, encoding="utf-8") as f:
            user_ann = json.load(f)
        overrides = user_ann.get("overrides", {})
        applied = 0
        for p in papers:
            if p["id"] in overrides:
                p["classification"]["user_override"] = overrides[p["id"]]
                applied += 1
        if applied:
            print(f"  Applied {applied} user overrides from {annotations_path}",
                  file=sys.stderr)

    counts = {
        "interested": sum(1 for p in papers if p["classification"]["label"] == "interested"),
        "not_for_me": sum(1 for p in papers if p["classification"]["label"] == "not_for_me"),
        "neutral": sum(1 for p in papers if p["classification"]["label"] == "neutral"),
    }

    data = {
        "fetched_date": today,
        "arxiv_date": arxiv_date,
        "total": len(papers),
        "counts": counts,
        "papers": papers,
    }

    dated_path = f"{out_dir}/{today}.json"
    latest_path = f"{out_dir}/latest.json"

    for path in (dated_path, latest_path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Saved → {dated_path}", file=sys.stderr)
    print(
        f"Interested: {counts['interested']}  "
        f"Not for me: {counts['not_for_me']}  "
        f"Neutral: {counts['neutral']}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
