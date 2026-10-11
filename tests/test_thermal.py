import asyncio

from olympus import config as cfg
from olympus.thermal import Reading, Thermal, read


def test_read_parses_hwmon(tmp_path):
    for i, (name, labels) in enumerate([("amdgpu", {"edge": 50, "junction": 60, "mem": 70}), ("coretemp", {"Package id 0": 55})]):
        h = tmp_path / f"hwmon{i}"
        h.mkdir()
        (h / "name").write_text(name)
        for j, (label, v) in enumerate(labels.items(), 1):
            (h / f"temp{j}_input").write_text(str(v * 1000))
            (h / f"temp{j}_label").write_text(label)
    r = read(tmp_path)
    assert r.temps == {"gpu_edge": 50, "gpu_junction": 60, "gpu_mem": 70, "cpu": 55}


def test_limits_and_wait(home, monkeypatch):
    c = cfg.load()
    temps = iter([97, 97, 90, 80])  # het → fortsatt over grense-hysterese → kald
    th = Thermal(c, reader=lambda: Reading({"gpu_junction": next(temps), "cpu": 50}))
    real_sleep = asyncio.sleep
    monkeypatch.setattr("olympus.thermal.asyncio.sleep", lambda s: real_sleep(0))
    msgs = []
    assert asyncio.run(th.wait_cool(msgs.append, timeout=5))
    assert msgs and "GPU 97" in msgs[0]
    hot = Thermal(c, reader=lambda: Reading({"gpu_junction": 101.0}))
    assert hot.critical() and hot.short()[1] == "het"
    assert Thermal(c, reader=lambda: Reading({"gpu_junction": 60.0, "cpu": 50.0})).short() == ("GPU 60° CPU 50°", "ok")
