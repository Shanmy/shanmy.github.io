#!/usr/bin/env python3
"""
Fetch and classify cs.CV papers from arxiv recent listings.
For 'interested' papers, optionally call Gemini API for structured analysis.

Env vars:
  GEMINI_API_KEY  – enables paper analysis (optional)

Output:
  personal/arxiv/YYYY-MM-DD.json
  personal/arxiv/latest.json
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

ARXIV_URL = "https://arxiv.org/list/cs.CV/recent"

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
            return {"label": "not_interested", "auto_tags": not_tags}
        return {
            "label": "interested",
            "auto_tags": int_tags,
            "caution_tags": [t for t in not_tags if t != "medical"],
        }

    if not_tags:
        return {"label": "not_interested", "auto_tags": not_tags}

    return {"label": "neutral", "auto_tags": []}


# ── Scraper ───────────────────────────────────────────────────────────────────

def fetch_papers() -> tuple[list[dict], str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (academic research reader)"
        )
    }
    resp = requests.get(ARXIV_URL, headers=headers, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    arxiv_date = ""
    for h3 in soup.find_all("h3"):
        text = h3.get_text(strip=True)
        if "submissions" in text.lower() or re.search(r"\w+ \d+,? \d{4}", text):
            arxiv_date = text
            break

    papers = []
    dl = soup.find("dl", id="articles")
    if not dl:
        print("Warning: could not find #articles dl", file=sys.stderr)
        return papers, arxiv_date

    dts = dl.find_all("dt")
    dds = dl.find_all("dd")

    for dt, dd in zip(dts, dds):
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
        authors = []
        if authors_div:
            authors = [a.get_text(strip=True) for a in authors_div.find_all("a")]

        abstract = ""
        abstract_p = dd.find("p", class_="mathjax")
        if abstract_p:
            abstract = abstract_p.get_text(" ", strip=True)

        papers.append({
            "id": arxiv_id,
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "link": link,
            "cross_listed": is_cross,
            "classification": classify(title, abstract),
            "analysis": None,  # filled in later for interested papers
        })

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
    print("Fetching papers from arxiv cs.CV/recent …", file=sys.stderr)
    papers, arxiv_date = fetch_papers()

    if not papers:
        print("No papers found – aborting.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(papers)} papers  (header: {arxiv_date!r})", file=sys.stderr)

    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if gemini_key:
        analyze_interested_papers(papers, gemini_key)
    else:
        print("GEMINI_API_KEY not set – skipping analysis.", file=sys.stderr)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir = "personal/arxiv"
    os.makedirs(out_dir, exist_ok=True)

    counts = {
        "interested": sum(1 for p in papers if p["classification"]["label"] == "interested"),
        "not_interested": sum(1 for p in papers if p["classification"]["label"] == "not_interested"),
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
        f"Not interested: {counts['not_interested']}  "
        f"Neutral: {counts['neutral']}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
