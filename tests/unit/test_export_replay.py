"""Replay export from a small hand-written run in the real log format."""

import json

from experiments import export_replay
from experiments.harness import page_url

T0 = 1000.0  # monotonic time of scenario t=0


def ev(event, t=None, mono=None, **fields):
    e = {"event": event, "mono": mono if mono is not None else T0 + (t or 0.0), **fields}
    if t is not None:
        e["t"] = t
    return e


def write_run(root, with_new_events=True):
    run = root / "results" / "b" / "5" / "car_tunnel_45s" / "s0"
    run.mkdir(parents=True)
    (run / "meta.json").write_text(json.dumps(
        {"config": "5", "about": "full", "scenario": "car_tunnel_45s", "session": 0, "done": True}
    ))  # fmt: skip
    a, b = page_url("Albert_Einstein"), page_url("Physics")
    client = [
        ev("run_start", mono=T0 - 2, active="cell0", config="5"),
        ev("click", t=1.0, i=0, url=a),
        ev("view", t=1.0, i=0, url=a, status=200, source="origin", wait_s=0.4, bytes=250_000),
        ev("netem", mono=T0 + 38, msg="t=38.0 cell0 -> dead"),
        ev("netem", mono=T0 + 84, msg="t=84.0 cell0 -> 5g"),
        ev("click", t=40.0, i=1, url=b),
        ev("view", t=40.0, i=1, url=b, status=200, source="push", wait_s=0.01, bytes=90_000),
        ev("click", t=70.0, i=2, url=page_url("Missing")),
        ev("run_end", t=120.0),
    ]
    if with_new_events:
        client[1:1] = [
            ev("clock", t=0.0),
            ev("signal", t=0.0, active="cell0", dbm={"wifi0": -140, "cell0": -85, "sat0": -140}),
            ev("warning", t=33.0, iface="cell0", on=True, eta_s=4.0),
            ev("hint_sent", t=33.1, active=True, eta_s=4.0, outage_s=30.0),
            ev("ping", t=0.2, iface="cell0", rtt_ms=31.5),
            ev("ping", t=38.5, iface="cell0", rtt_ms=None),
        ]
    server = [
        {"event": "push", "mono": T0 + 34.0, "url": b, "wire_bytes": 30_000, "prob": 0.4, "depth": 1},
        {"event": "push", "mono": T0 + 35.0, "url": page_url("X"), "wire_bytes": 20_000, "prob": 0.1,
         "depth": 2},
    ]  # fmt: skip
    for name, events in (("client", client), ("server", server)):
        (run / f"{name}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    return run


def test_export_run(tmp_path):
    r = export_replay.export_run(write_run(tmp_path))
    assert r["config"] == "5" and r["duration"] == 120.0
    assert r["zones"]["cell0"] == [[0.0, 38, "5g"], [38, 84, "dead"], [84, 120.0, "5g"]]
    assert r["zones"]["wifi0"] == [[0.0, 120.0, "dead"]]
    assert r["outage"] == [38.0, 94.0]
    assert r["signal"]["t"] == [0.0] and r["signal"]["dbm"]["cell0"] == [-85]
    assert r["pings"] == [[0.2, 31.5, "cell0"], [38.5, None, "cell0"]]
    assert r["hints"] == [{"t": 33.1, "on": True}]
    assert [p["source"] for p in r["pages"]] == ["origin", "push", "pending"]
    assert r["pages"][0]["title"] == "Albert Einstein"
    assert r["pages"][2]["wait"] == 50.0  # still waiting when the run ended at 120 s
    assert [(p["t"], p["depth"], p["opened"]) for p in r["pushes"]] == [
        (34.0, 1, True),
        (35.0, 2, False),
    ]
    assert r["metrics"]["outage_cached"] == 1


def test_older_runs_rebuild_signal_and_clock(tmp_path):
    r = export_replay.export_run(write_run(tmp_path, with_new_events=False))
    assert r["signal"]["t"][:3] == [0.0, 0.5, 1.0]
    assert r["signal"]["dbm"]["cell0"][0] == -85.0  # the scenario's trace at t=0
    assert r["pings"] == [] and r["warnings"] == []
    assert r["pushes"][0]["t"] == 34.0  # clock recovered from the clicks


def test_main_writes_files_and_index(tmp_path, monkeypatch):
    write_run(tmp_path)
    monkeypatch.setattr(export_replay, "ROOT", tmp_path)
    out = tmp_path / "site" / "replays"
    monkeypatch.setattr("sys.argv", ["x", "b", "--out", str(out)])
    export_replay.main()
    index = json.loads((out / "index.json").read_text())
    (run,) = index["runs"]
    assert run["file"] == "b/car_tunnel_45s/s0/5.json" and (out / run["file"]).exists()
    export_replay.main()  # re-export replaces, doesn't duplicate
    assert len(json.loads((out / "index.json").read_text())["runs"]) == 1
