from olympus.codefiles import extract_files, save_files

GROQ_STYLE = """**Enkel statisk nettside**

### `index.html`

```html
<!DOCTYPE html>
<html></html>
```

### `style.css`
```css
body { margin: 0; }
```

Kjør så:

```bash
xdg-open index.html
```
"""


def test_finds_files_from_headings_and_skips_unnamed_blocks():
    files = extract_files(GROQ_STYLE)
    assert [f for f, _ in files] == ["index.html", "style.css"]
    assert files[1][1] == "body { margin: 0; }\n"


def test_info_string_paths():
    md = '```html nettside/index.html\n<p>hei</p>\n```\n\n```python title="app.py"\nprint(1)\n```\n'
    assert [f for f, _ in extract_files(md)] == ["nettside/index.html", "app.py"]


def test_bold_and_fil_labels():
    md = "**script.js**\n```js\nlet a = 1\n```\n\nFil: main.py\n```python\npass\n```\n"
    assert [f for f, _ in extract_files(md)] == ["script.js", "main.py"]


def test_rejects_absolute_and_parent_paths():
    md = "```sh /etc/passwd\nx\n```\n```sh ../ut.sh\nx\n```\n"
    assert extract_files(md) == []


def test_save_files_stays_inside_base(tmp_path):
    written = save_files(tmp_path, [("nettside/index.html", "<p>hei</p>\n")])
    assert written == [tmp_path / "nettside" / "index.html"]
    assert written[0].read_text() == "<p>hei</p>\n"


def test_filename_in_sentence_above_block():
    md = "Jeg kan ikke skrive filer her, så du må lagre koden selv som `index.html`.\n\n```html\n<p/>\n```\n"
    assert [f for f, _ in extract_files(md)] == ["index.html"]
    md2 = "Bytt `a.py` med `b.py`:\n```python\nx\n```\n"  # to navn: tvetydig, ikke gjett
    assert extract_files(md2) == []


def test_rejects_dotfiles_and_keeps_existing(tmp_path):
    from olympus.codefiles import existing

    assert extract_files("```sh .bashrc\nrm -rf ~\n```\n") == []
    assert extract_files("```ini .config/hypr/x.conf\nx\n```\n") == []
    (tmp_path / "index.html").write_text("min egen\n")
    files = [("index.html", "ny\n"), ("style.css", "body{}\n")]
    assert existing(tmp_path, files) == ["index.html"]
    save_files(tmp_path, files)
    assert (tmp_path / "index.html").read_text() == "min egen\n"   # ikke overskrevet uten bekreftelse
    assert (tmp_path / "style.css").exists()
    save_files(tmp_path, files, overwrite=True)
    assert (tmp_path / "index.html").read_text() == "ny\n"


def test_symlink_out_of_base_is_refused(tmp_path):
    outside = tmp_path / "ute"
    outside.mkdir()
    base = tmp_path / "base"
    base.mkdir()
    (base / "lenke").symlink_to(outside)
    assert save_files(base, [("lenke/x.txt", "x\n")]) == []
    assert not (outside / "x.txt").exists()
