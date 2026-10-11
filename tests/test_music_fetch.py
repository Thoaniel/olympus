"""Zeus henter fra en lenke via media add (yt-dlp, torrent eller direkte), alltid med brukerens godkjenning og aldri
fra interne adresser."""

import json

import pytest

from olympus.tools import ToolCall, Toolbox


@pytest.mark.asyncio
async def test_media_add_rejects_bad_links(tmp_path):
    class Dl:
        started = []

        def start(self, *a, **k):
            self.started.append(a)
            return {"id": "x"}

        start_ytdlp = start

    tb = Toolbox(tmp_path)
    tb.dl = Dl()
    for bad in ("--exec=rm -rf ~", "file:///etc/passwd", "ftp://x/y", "javascript:alert(1)"):  # ugyldige lenker
        call = ToolCall("1", "media", json.dumps({"action": "add", "url": bad, "kind": "music"}))
        r = await tb.run(call)
        assert not r.ok, bad
    assert Dl.started == []
