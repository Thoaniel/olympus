"""E-bøker til leseren: ryddig tekst (avsnitt, ikke kildekodens linjeskift), kapitler fra innholdsfortegnelsen, og
PDF-konverterte bøker (én <p> per side, topptekst på hver side) samlet til ekte kapitler."""
import zipfile

from olympus.medialib import ebook_text

OPF = """<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf"><metadata/><manifest>
{items}</manifest><spine>{spine}</spine></package>"""


def make_epub(path, files: dict[str, str], nav: str | None = None):
    items = "".join(f'<item id="i{n}" href="{f}" media-type="application/xhtml+xml"/>' for n, f in enumerate(files))
    if nav:
        items += '<item id="nav" href="nav.xhtml" properties="nav" media-type="application/xhtml+xml"/>'
    spine = "".join(f'<itemref idref="i{n}"/>' for n in range(len(files)))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="OPS/content.opf"/></rootfiles></container>')
        z.writestr("OPS/content.opf", OPF.format(items=items, spine=spine))
        for f, html in files.items():
            z.writestr(f"OPS/{f}", html)
        if nav:
            z.writestr("OPS/nav.xhtml", nav)


def test_paragraphs_and_toc_titles(tmp_path):
    p = tmp_path / "bok.epub"
    make_epub(p, {
        "a.xhtml": "<html><body><p>Første linje\n      fortsetter her.</p><p>Andre</p><p>avsnitt.</p></body></html>",
        "b.xhtml": "<html><body><p>Mer tekst i kapittel én.</p></body></html>",
        "c.xhtml": "<html><body><h2>Overskrift i fila</h2><ul><li>en</li><li>to</li></ul></body></html>",
    }, nav='<nav epub:type="toc"><ol><li><a href="a.xhtml">Kapittel 1: Start</a></li><li><a href="c.xhtml#x">Kapittel 2</a></li></ol></nav>')
    ch = ebook_text(p)
    assert [t for t, _ in ch] == ["Kapittel 1: Start", "Kapittel 2"]  # b.xhtml hører til kapittel 1
    assert ch[0][1].split("\n\n")[:3] == ["Første linje fortsetter her.", "Andre", "avsnitt."]
    assert "Mer tekst i kapittel én." in ch[0][1]
    assert "• en" in ch[1][1] and "• to" in ch[1][1]


def test_page_scan_book_gets_real_chapters(tmp_path):
    head = "MY MANUAL Reprinted with permission by U.S. Department of the Army "
    pages = {"page_0.html": f"<p>{head}TABLE OF CONTENTS CHAPTER 1 - START 5 BASICS 6 CHAPTER 2 - WATER 9</p>"}
    body = ["CHAPTER 1 - START This is how it begins. " + "Some words here. " * 30,
            "More of chapter one. FIRST AID Bleeding must be stopped quickly. " + "Text. " * 20,
            "CHAPTER 2 - WATER 4. Water is life. " + "Drink it. " * 20 + "• ^: ^ Scan noise before this."] + ["Filler page. " * 10] * 40
    for n, t in enumerate(body, 1):
        pages[f"page_{n}.html"] = f"<html><head><title>Page {n}</title></head><body><p>{head}{t}</p><p>Page {n} of 44</p></body></html>"
    p = tmp_path / "scan.epub"
    make_epub(p, pages)
    ch = ebook_text(p)
    titles = [t for t, _ in ch]
    assert titles == ["Start", "Chapter 1 - Start", "Chapter 2 - Water"]  # innholdsfortegnelsen gir ingen kapitler
    text = ch[1][1]
    assert "MY MANUAL" not in text and "Department of the Army" not in text and "Page 2 of 44" not in text
    assert "\n\nFIRST AID\n\n" in text  # underoverskrift på egen linje
    assert "Scan noise before this." in ch[2][1] and "^:" not in ch[2][1]  # skannestøy fjernet
