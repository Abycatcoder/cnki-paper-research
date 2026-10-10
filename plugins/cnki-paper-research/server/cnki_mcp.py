#!/usr/bin/env python3
"""Read-only MCP server for CNKI pages and public scholarly metadata fallbacks."""

from __future__ import annotations

import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from typing import Any

SERVER_NAME = "cnki-paper-research"
SERVER_VERSION = "0.4.0"
CNKI_ORIGIN = "https://kns.cnki.net"
SEARCH_URL = CNKI_ORIGIN + "/kns8s/defaultresult/index"
CROSSREF_URL = "https://api.crossref.org/works"
OPENALEX_URL = "https://api.openalex.org/works"
USER_AGENT = f"{SERVER_NAME}/{SERVER_VERSION} (read-only research plugin)"


def clean_text(value: str | None) -> str | None:
    if not value:
        return None
    value = re.sub(r"<script\b[^>]*>.*?</script>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<style\b[^>]*>.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value).replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n:：;；")
    return value or None


def fetch(url: str, timeout: int = 25) -> tuple[str, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            "Referer": "https://www.cnki.net/",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        try:
            text = body.decode(charset, errors="replace")
        except LookupError:
            text = body.decode("utf-8", errors="replace")
        return response.geturl(), text


def fetch_json(url: str, timeout: int = 25) -> tuple[str, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
        if not isinstance(payload, dict):
            raise ValueError("公共数据源返回了非对象 JSON")
        return response.geturl(), payload


def first_text(value: Any) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    return clean_text(str(value)) if value is not None else None


def normalize_doi(value: Any) -> str | None:
    doi = first_text(value)
    if not doi:
        return None
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.I)
    return doi.lower().strip()


def year_from_parts(value: Any) -> str | None:
    try:
        parts = value["date-parts"][0]
        return "-".join(str(part) for part in parts)
    except (KeyError, IndexError, TypeError):
        return None


def crossref_authors(item: dict[str, Any]) -> list[str]:
    authors: list[str] = []
    for author in item.get("author") or []:
        if not isinstance(author, dict):
            continue
        name = clean_text(" ".join(x for x in [str(author.get("given") or ""), str(author.get("family") or "")] if x))
        if name:
            authors.append(name)
    return authors


def search_crossref(query: str, limit: int, year_from: int | None, year_to: int | None) -> list[dict[str, Any]]:
    params: dict[str, str] = {
        "query.bibliographic": query,
        "rows": str(limit),
        "select": "DOI,title,author,container-title,published,issued,URL,type,ISSN,is-referenced-by-count,abstract,publisher",
    }
    filters = []
    if year_from:
        filters.append(f"from-pub-date:{year_from}-01-01")
    if year_to:
        filters.append(f"until-pub-date:{year_to}-12-31")
    if filters:
        params["filter"] = ",".join(filters)
    _, payload = fetch_json(CROSSREF_URL + "?" + urllib.parse.urlencode(params))
    records: list[dict[str, Any]] = []
    for item in (payload.get("message") or {}).get("items") or []:
        if not isinstance(item, dict):
            continue
        title = first_text(item.get("title"))
        if not title:
            continue
        published = year_from_parts(item.get("published")) or year_from_parts(item.get("issued"))
        records.append({
            "title": title,
            "authors": crossref_authors(item),
            "source": first_text(item.get("container-title")) or first_text(item.get("publisher")),
            "publication_date": published,
            "document_type": first_text(item.get("type")),
            "issn": first_text(item.get("ISSN")),
            "doi": normalize_doi(item.get("DOI")),
            "cited_count": item.get("is-referenced-by-count"),
            "cited_count_source": "Crossref",
            "abstract": clean_text(item.get("abstract")),
            "url": first_text(item.get("URL")),
            "metadata_source": "Crossref",
            "verification_status": "公开元数据已核验",
            "evidence_scope": "摘要" if item.get("abstract") else "元数据",
        })
    return records


def openalex_authors(item: dict[str, Any]) -> list[str]:
    authors: list[str] = []
    for authorship in item.get("authorships") or []:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author") or {}
        name = clean_text(author.get("display_name"))
        if name:
            authors.append(name)
    return authors


def search_openalex(query: str, limit: int, year_from: int | None, year_to: int | None) -> list[dict[str, Any]]:
    filters = []
    if year_from:
        filters.append(f"from_publication_date:{year_from}-01-01")
    if year_to:
        filters.append(f"to_publication_date:{year_to}-12-31")
    params = {"search": query, "per-page": str(limit)}
    if filters:
        params["filter"] = ",".join(filters)
    _, payload = fetch_json(OPENALEX_URL + "?" + urllib.parse.urlencode(params))
    records: list[dict[str, Any]] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        title = clean_text(item.get("display_name") or item.get("title"))
        if not title:
            continue
        primary = item.get("primary_location") or {}
        source = primary.get("source") or {}
        doi = normalize_doi(item.get("doi"))
        records.append({
            "title": title,
            "authors": openalex_authors(item),
            "source": clean_text(source.get("display_name")),
            "publication_date": first_text(item.get("publication_date") or item.get("publication_year")),
            "document_type": first_text(item.get("type_crossref") or item.get("type")),
            "issn": first_text(source.get("issn")),
            "doi": doi,
            "cited_count": item.get("cited_by_count"),
            "cited_count_source": "OpenAlex",
            "abstract": None,
            "url": first_text(primary.get("landing_page_url")) or first_text(item.get("id")),
            "metadata_source": "OpenAlex",
            "verification_status": "公开元数据已核验",
            "evidence_scope": "元数据",
        })
    return records


def record_key(record: dict[str, Any]) -> str:
    doi = normalize_doi(record.get("doi"))
    if doi:
        return "doi:" + doi
    title = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(record.get("title") or "").lower())
    return "title:" + title


def merge_public_records(records: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for record in records:
        key = record_key(record)
        if key == "title:":
            continue
        if key not in merged:
            record["metadata_sources"] = [record.pop("metadata_source")]
            merged[key] = record
            continue
        current = merged[key]
        source_name = record.get("metadata_source")
        if source_name and source_name not in current["metadata_sources"]:
            current["metadata_sources"].append(source_name)
        for field in ("authors", "source", "publication_date", "document_type", "issn", "doi", "abstract", "url"):
            if not current.get(field) and record.get(field):
                current[field] = record[field]
    return list(merged.values())[:limit]


def meta_value(page: str, names: list[str]) -> str | None:
    for name in names:
        patterns = [
            rf'<meta[^>]+(?:name|property)=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']*)',
            rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:name|property)=["\']{re.escape(name)}["\']',
        ]
        for pattern in patterns:
            match = re.search(pattern, page, flags=re.I)
            if match:
                return clean_text(match.group(1))
    return None


def labelled_value(page_text: str, labels: list[str], max_len: int = 500) -> str | None:
    for label in labels:
        match = re.search(
            rf"(?:{re.escape(label)})\s*[：:]\s*(.{{1,{max_len}}}?)(?=\s(?:作者|机构|摘要|关键词|基金|DOI|分类号|文献来源|来源|发表时间|网络首发|页码|$)[：:]?)",
            page_text,
            flags=re.I,
        )
        if match:
            return clean_text(match.group(1))
    return None


def parse_detail(url: str, page: str) -> dict[str, Any]:
    plain = clean_text(page) or ""
    title = meta_value(page, ["citation_title", "dc.title", "og:title"])
    if not title:
        match = re.search(r"<h1[^>]*>(.*?)</h1>", page, flags=re.I | re.S)
        title = clean_text(match.group(1)) if match else None

    authors = re.findall(
        r'<meta[^>]+name=["\']citation_author["\'][^>]+content=["\']([^"\']+)',
        page,
        flags=re.I,
    )
    if not authors:
        raw_authors = meta_value(page, ["dc.creator", "author"])
        authors = re.split(r"[,，;；\s]+", raw_authors) if raw_authors else []

    abstract = meta_value(page, ["description", "dc.description", "citation_abstract"])
    if not abstract:
        abstract = labelled_value(plain, ["摘要"], 2500)

    keywords = meta_value(page, ["keywords", "citation_keywords"])
    source = meta_value(page, ["citation_journal_title", "dc.source"])
    published = meta_value(page, ["citation_publication_date", "citation_date", "dc.date"])
    doi = meta_value(page, ["citation_doi", "dc.identifier"])
    volume = meta_value(page, ["citation_volume"])
    issue = meta_value(page, ["citation_issue"])
    first_page = meta_value(page, ["citation_firstpage"])
    last_page = meta_value(page, ["citation_lastpage"])

    if not doi:
        match = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", plain, flags=re.I)
        doi = match.group(0).rstrip("。。;；,)）") if match else None

    def id_match(pattern: str) -> str | None:
        found = re.search(pattern, plain, flags=re.I)
        return clean_text(found.group(1)) if found else None

    cited = id_match(r"(?:被引次数|被引量|被引)\s*[：:]?\s*(\d+)")
    cn = id_match(r"(?:CN刊号|国内统一刊号|CN)\s*[：:]?\s*([0-9]{2}-[0-9A-Z]{3,6}(?:/[A-Z])?)")
    issn = id_match(r"ISSN\s*[：:]?\s*([0-9]{4}-[0-9X]{4})")
    pages = None
    if first_page:
        pages = first_page + (("-" + last_page) if last_page and last_page != first_page else "")

    return {
        "title": title,
        "authors": [clean_text(a) for a in authors if clean_text(a)],
        "source": source,
        "publication_date": published,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "cn": cn,
        "issn": issn,
        "doi": doi,
        "cited_count": int(cited) if cited and cited.isdigit() else None,
        "keywords": [x for x in re.split(r"[,，;；]", keywords or "") if clean_text(x)],
        "abstract": abstract,
        "evidence_scope": "摘要" if abstract else "元数据",
        "url": url,
        "retrieved_at": date.today().isoformat(),
    }


def parse_search_results(page: str, limit: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    anchor_pattern = re.compile(
        r'<a\b([^>]*?href=["\']([^"\']*(?:detail|Detail)[^"\']*)["\'][^>]*)>(.*?)</a>',
        flags=re.I | re.S,
    )
    for match in anchor_pattern.finditer(page):
        title = clean_text(match.group(3))
        if not title or len(title) < 3:
            continue
        url = urllib.parse.urljoin(CNKI_ORIGIN, html.unescape(match.group(2)))
        if url in seen:
            continue
        seen.add(url)
        context = clean_text(page[max(0, match.start() - 900): min(len(page), match.end() + 1400)]) or ""
        year_match = re.search(r"(?:19|20)\d{2}(?:[-/.年]\d{1,2})?", context)
        cite_match = re.search(r"(?:被引|引用)\s*[：:]?\s*(\d+)", context)
        results.append({
            "title": title,
            "authors": None,
            "source": None,
            "publication_date": year_match.group(0) if year_match else None,
            "cited_count": int(cite_match.group(1)) if cite_match else None,
            "url": url,
            "evidence_scope": "元数据",
        })
        if len(results) >= limit:
            break
    return results


def search_papers(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).strip()
    if not query:
        raise ValueError("query 不能为空")
    limit = max(1, min(int(args.get("limit", 10)), 30))
    field = str(args.get("search_field", "topic"))
    korder = {"topic": "SU", "title": "TI", "author": "AU"}.get(field, "SU")
    params = {"kw": query, "korder": korder}
    request_url = SEARCH_URL + "?" + urllib.parse.urlencode(params)
    try:
        final_url, page = fetch(request_url)
    except (urllib.error.URLError, TimeoutError) as exc:
        return {
            "query": query,
            "results": [],
            "status": "direct_access_failed",
            "message": "MCP 直连知网失败；请把 search_url 交给已授权浏览器继续检索。",
            "error": str(exc),
            "search_url": request_url,
            "retrieved_at": date.today().isoformat(),
        }
    lower = page.lower()
    if "captcha" in lower or "验证码" in page or "verify you are human" in lower:
        return {
            "query": query,
            "results": [],
            "status": "captcha_required",
            "message": "知网页面要求人工完成验证码。插件不会绕过验证，请在浏览器中打开检索链接后完成验证。",
            "search_url": final_url,
            "retrieved_at": date.today().isoformat(),
        }
    results = parse_search_results(page, limit)
    year_from = args.get("year_from")
    year_to = args.get("year_to")
    if year_from or year_to:
        filtered = []
        for item in results:
            match = re.search(r"(?:19|20)\d{2}", item.get("publication_date") or "")
            if not match:
                continue
            year = int(match.group(0))
            if year_from and year < int(year_from):
                continue
            if year_to and year > int(year_to):
                continue
            filtered.append(item)
        results = filtered
    status = "ok" if results else "no_visible_results"
    message = None if results else "页面未暴露可解析的论文条目，可能需要登录、浏览器会话或人工验证。"
    return {
        "query": query,
        "search_field": field,
        "results": results,
        "status": status,
        "message": message,
        "search_url": final_url,
        "retrieved_at": date.today().isoformat(),
    }


def get_paper(args: dict[str, Any]) -> dict[str, Any]:
    url = str(args.get("url", "")).strip()
    if not url:
        raise ValueError("url 不能为空")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.hostname.endswith("cnki.net"):
        raise ValueError("只允许读取 cnki.net 的论文页面")
    try:
        final_url, page = fetch(url)
    except (urllib.error.URLError, TimeoutError) as exc:
        return {
            "status": "direct_access_failed",
            "url": url,
            "message": "MCP 直连论文详情页失败；请使用已授权浏览器打开该链接。",
            "error": str(exc),
            "retrieved_at": date.today().isoformat(),
        }
    lower = page.lower()
    if "captcha" in lower or "验证码" in page:
        return {"status": "captcha_required", "url": final_url, "message": "知网页面要求人工验证。"}
    data = parse_detail(final_url, page)
    data["status"] = "ok" if data.get("title") else "partial"
    return data


def search_public_sources(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).strip()
    if not query:
        raise ValueError("query 不能为空")
    limit = max(1, min(int(args.get("limit", 10)), 30))
    year_from = int(args["year_from"]) if args.get("year_from") else None
    year_to = int(args["year_to"]) if args.get("year_to") else None
    requested = args.get("sources") or ["crossref", "openalex"]
    records: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    attempted: list[str] = []
    source_functions = {
        "crossref": search_crossref,
        "openalex": search_openalex,
    }
    for source_name in requested:
        if source_name not in source_functions or source_name in attempted:
            continue
        attempted.append(source_name)
        try:
            records.extend(source_functions[source_name](query, limit, year_from, year_to))
        except (ValueError, urllib.error.URLError, TimeoutError) as exc:
            errors[source_name] = str(exc)
    merged = merge_public_records(records, limit)
    if merged and errors:
        status = "partial"
    elif merged:
        status = "ok"
    else:
        status = "unavailable"
    return {
        "query": query,
        "status": status,
        "results": merged,
        "sources_attempted": attempted,
        "source_errors": errors,
        "notice": "这些结果来自公开学术元数据源，不等同于知网结果；引用量必须连同 cited_count_source 展示。",
        "retrieved_at": date.today().isoformat(),
    }


TOOLS = [
    {
        "name": "search_papers",
        "title": "检索知网论文",
        "description": "按主题、标题或作者检索知网公开可见的论文元数据。遇到登录或验证码时返回明确状态，不绕过访问控制。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "论文关键词、标题或作者名"},
                "search_field": {"type": "string", "enum": ["topic", "title", "author"], "default": "topic"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10},
                "year_from": {"type": "integer", "minimum": 1900, "maximum": 2100},
                "year_to": {"type": "integer", "minimum": 1900, "maximum": 2100}
            },
            "required": ["query"],
            "additionalProperties": False
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True, "destructiveHint": False}
    },
    {
        "name": "get_paper",
        "title": "读取知网论文详情",
        "description": "读取一个 CNKI 论文详情页中公开可见的标题、作者、来源、日期、刊号、DOI、被引次数、关键词和摘要。",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "cnki.net 论文详情页 URL"}},
            "required": ["url"],
            "additionalProperties": False
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True, "destructiveHint": False}
    },
    {
        "name": "search_public_sources",
        "title": "检索公开学术来源",
        "description": "当知网直连或浏览器检索不可用时，检索 Crossref 与 OpenAlex 公开元数据。结果不冒充知网数据，并保留来源与引用量口径。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "论文关键词或标题"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10},
                "year_from": {"type": "integer", "minimum": 1900, "maximum": 2100},
                "year_to": {"type": "integer", "minimum": 1900, "maximum": 2100},
                "sources": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["crossref", "openalex"]},
                    "default": ["crossref", "openalex"]
                }
            },
            "required": ["query"],
            "additionalProperties": False
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True, "destructiveHint": False}
    }
]


def tool_result(data: dict[str, Any], is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}],
        "structuredContent": data,
        "isError": is_error,
    }


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    if request_id is None:
        return None
    if method == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": "知网论文检索必须经由浏览器完成：优先调用宿主内置浏览器并在右侧展示检索状态，内置不可用时调用云端浏览器。search_papers 仅用于直连预检，其 search_url 应交给浏览器继续；浏览器均不可用时调用 search_public_sources。只总结可验证内容，公开来源不得冒充知网。"
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "search_papers":
                result = tool_result(search_papers(args))
            elif name == "get_paper":
                result = tool_result(get_paper(args))
            elif name == "search_public_sources":
                result = tool_result(search_public_sources(args))
            else:
                result = tool_result({"error": f"未知工具: {name}"}, True)
        except (ValueError, urllib.error.URLError, TimeoutError) as exc:
            result = tool_result({"status": "error", "error": str(exc)}, True)
        except Exception as exc:  # Keep server alive on unexpected page changes.
            result = tool_result({"status": "error", "error": f"检索失败: {exc}"}, True)
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            response = handle(request)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                sys.stdout.flush()
        except Exception as exc:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
