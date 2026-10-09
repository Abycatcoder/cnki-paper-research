#!/usr/bin/env python3
import importlib.util
import json
import pathlib
import re
import unittest
import urllib.error
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("cnki_mcp", ROOT / "server" / "cnki_mcp.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_initialize_and_tools():
    initialized = MODULE.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert initialized["result"]["serverInfo"]["name"] == "cnki-paper-research"
    listed = MODULE.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert initialized["result"]["serverInfo"]["version"] == "0.3.1"
    assert {tool["name"] for tool in listed["result"]["tools"]} == {
        "search_papers",
        "get_paper",
        "search_public_sources",
    }


def test_parse_detail_metadata():
    page = """
    <html><head>
      <meta name="citation_title" content="生成式人工智能教育应用研究">
      <meta name="citation_author" content="张三">
      <meta name="citation_journal_title" content="现代教育技术">
      <meta name="citation_publication_date" content="2026-03-01">
      <meta name="citation_doi" content="10.1234/example.2026.01">
      <meta name="description" content="本文研究生成式人工智能在教育中的应用。">
    </head><body>ISSN：1234-567X CN：11-1234/G4 被引次数：12</body></html>
    """
    result = MODULE.parse_detail("https://kns.cnki.net/example", page)
    assert result["title"] == "生成式人工智能教育应用研究"
    assert result["authors"] == ["张三"]
    assert result["doi"] == "10.1234/example.2026.01"
    assert result["cited_count"] == 12
    assert result["evidence_scope"] == "摘要"


def test_cnki_failure_returns_browser_handoff_url():
    with mock.patch.object(MODULE, "fetch", side_effect=urllib.error.URLError("blocked")):
        result = MODULE.search_papers({"query": "生成式人工智能教育应用"})
    assert result["status"] == "direct_access_failed"
    assert result["search_url"].startswith("https://kns.cnki.net/")


def test_public_source_merge_keeps_provenance_and_count_source():
    crossref = [{
        "title": "生成式人工智能教育应用研究",
        "authors": ["张三"],
        "source": "现代教育技术",
        "publication_date": "2026",
        "doi": "10.1234/example",
        "cited_count": 4,
        "cited_count_source": "Crossref",
        "metadata_source": "Crossref",
        "verification_status": "公开元数据已核验",
        "evidence_scope": "元数据",
        "url": "https://doi.org/10.1234/example",
    }]
    openalex = [{
        "title": "生成式人工智能教育应用研究",
        "authors": ["张三"],
        "source": None,
        "publication_date": "2026",
        "doi": "https://doi.org/10.1234/example",
        "cited_count": 6,
        "cited_count_source": "OpenAlex",
        "metadata_source": "OpenAlex",
        "verification_status": "公开元数据已核验",
        "evidence_scope": "元数据",
        "url": "https://openalex.org/W1",
    }]
    with mock.patch.object(MODULE, "search_crossref", return_value=crossref), mock.patch.object(
        MODULE, "search_openalex", return_value=openalex
    ):
        result = MODULE.search_public_sources({"query": "生成式人工智能教育应用"})
    assert result["status"] == "ok"
    assert len(result["results"]) == 1
    assert result["results"][0]["metadata_sources"] == ["Crossref", "OpenAlex"]
    assert result["results"][0]["cited_count_source"] == "Crossref"


def test_package_versions_and_relative_paths():
    manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["version"] == MODULE.SERVER_VERSION == "0.3.1"
    extension = manifest["extensions"]["org.cnki-paper-research"]
    assert (ROOT / extension["skills"] / "cnki-paper-research" / "SKILL.md").is_file()
    assert (ROOT / extension["mcpServers"]).is_file()
    for name in ("mcp.json", ".mcp.json"):
        config = json.loads((ROOT / name).read_text(encoding="utf-8"))
        relative = config["mcpServers"]["cnki"]["args"][0].replace("${PLUGIN_ROOT}/", "")
        assert (ROOT / relative).is_file()
    assert MODULE.USER_AGENT.startswith(f"{MODULE.SERVER_NAME}/{MODULE.SERVER_VERSION}")


def test_four_section_output_contract():
    skill = ROOT / "skills" / "cnki-paper-research"
    instructions = (skill / "SKILL.md").read_text(encoding="utf-8")
    schema = (skill / "references" / "output-schema.md").read_text(encoding="utf-8")
    headings = re.findall(r"^## (.+)$", schema, flags=re.M)
    assert headings == ["一、检索说明", "二、检索结果", "三、单篇论文解读", "四、跨论文综合"]
    main_header = next(line for line in schema.splitlines() if line.startswith("| # |"))
    assert len(main_header.strip("|").split("|")) == 8
    assert "**推荐引用（GB/T 7714）**" in schema
    assert "仅元数据" in schema and "至少两篇" in schema
    assert "不得新增第五部分" in schema
    assert "Read `references/output-schema.md`" in instructions


if __name__ == "__main__":
    suite = unittest.TestSuite(
        unittest.FunctionTestCase(function)
        for name, function in sorted(globals().items())
        if name.startswith("test_") and callable(function)
    )
    outcome = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if outcome.wasSuccessful() else 1)
