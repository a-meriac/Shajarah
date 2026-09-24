"""Run the system experiments: every (config × scenario × session) in the emulated network.

Per run: build the namespaces, start the origin (ep-org) and the server proxy (ep-srv), run the
client program (ep-cli) for the scenario's length, stop everything, tear the namespaces down.
Each run writes results/<batch>/<config>/<scenario>/s<session>/{client,server}.jsonl and
meta.json. Finished runs are skipped, so an interrupted batch continues where it stopped.

Needs root (namespaces, tc) and Jev answers warmed beforehand (python -m experiments.warm_jev).

  sudo .venv-linux/bin/python -m experiments.runner --batch main
  sudo .venv-linux/bin/python -m experiments.runner --batch smoke --configs 5 \
      --scenarios car_tunnel_45s --sessions 1
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

from edgeproxy.tunnel.certs import generate_self_signed
from experiments.harness import (
    CERTDIR,
    CONFIGS,
    ORIGIN_HOST,
    ORIGIN_PORT,
    ROOT,
    SCENARIOS,
    SERVER_HOST,
    SERVER_PORT,
    load_sessions,
)

PY = sys.executable
NETNS = ROOT / "emulation" / "netns_setup.sh"
MIN_PAGES = 6  # shorter sessions end before the interesting part of a scenario


def _ns(ns: str, *cmd: str, **kw) -> subprocess.Popen:
    return subprocess.Popen(["ip", "netns", "exec", ns, *cmd], cwd=ROOT, **kw)


def _wait_for_line(proc: subprocess.Popen, text: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if text in line:
            return
        if not line and proc.poll() is not None:
            break
    raise RuntimeError(f"process didn't print {text!r} (exit code {proc.poll()})")


def _stop(proc: subprocess.Popen | None) -> None:
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


def git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "-c", f"safe.directory={ROOT}", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        dirty = subprocess.run(
            ["git", "-c", f"safe.directory={ROOT}", "status", "--porcelain"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        return out.stdout.strip() + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def run_one(out: Path, config, scenario: str, session: int, settings_file: Path | None) -> None:
    duration = yaml.safe_load((SCENARIOS / f"{scenario}.yaml").read_text())["duration_s"]
    run_id = f"{config.name}/{scenario}/s{session}"
    out.mkdir(parents=True, exist_ok=True)
    for f in ("client.jsonl", "server.jsonl"):  # a half-finished earlier attempt
        (out / f).unlink(missing_ok=True)
    extra = ["--settings", str(settings_file)] if settings_file else []
    origin = server = None
    subprocess.run([str(NETNS), "up"], check=True, capture_output=True)
    try:
        origin = _ns("ep-org", PY, "-m", "data.origin_server", "--root", "data/snapshot",
                     "--host", ORIGIN_HOST, "--port", str(ORIGIN_PORT),
                     stdout=subprocess.PIPE, text=True)  # fmt: skip
        _wait_for_line(origin, "origin serving", 15)
        server_cmd = [
            PY, "-m", "edgeproxy.server.proxy", "--transport", config.transport,
            "--host", SERVER_HOST, "--port", str(SERVER_PORT), "--certdir", str(CERTDIR),
            "--predictor", config.predictor, "--offline",
            "--log", str(out / "server.jsonl"), "--run-id", run_id, *extra,
        ]  # fmt: skip
        if config.fixed_policy:
            server_cmd.append("--fixed-policy")
        server = _ns("ep-srv", *server_cmd, stdout=subprocess.PIPE, text=True)
        _wait_for_line(server, "server proxy", 30)
        client = _ns("ep-cli", PY, "-m", "experiments.client_run", "--config", config.name,
                     "--scenario", scenario, "--session", str(session),
                     "--log", str(out / "client.jsonl"), "--run-id", run_id, *extra)  # fmt: skip
        code = client.wait(timeout=duration + 60)
        if code != 0:
            raise RuntimeError(f"client exited with {code}")
    finally:
        _stop(server)
        _stop(origin)
        subprocess.run([str(NETNS), "down"], capture_output=True, check=False)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", required=True, help="results/<batch>/")
    ap.add_argument("--configs", nargs="+", default=list(CONFIGS), choices=list(CONFIGS))
    ap.add_argument("--scenarios", nargs="+",
                    default=sorted(p.stem for p in SCENARIOS.glob("*.yaml")))  # fmt: skip
    ap.add_argument("--sessions", type=int, default=5, help="first N sessions long enough")
    ap.add_argument("--settings", type=Path, default=None, help="default: settings.yaml")
    args = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("needs root: sudo .venv-linux/bin/python -m experiments.runner ...")

    CERTDIR.mkdir(parents=True, exist_ok=True)
    if not (CERTDIR / "cert.pem").exists():
        generate_self_signed(CERTDIR / "cert.pem", CERTDIR / "key.pem")
    batch = ROOT / "results" / args.batch
    sessions = [i for i, s in enumerate(load_sessions()) if len(s["pages"]) >= MIN_PAGES]
    sessions = sessions[: args.sessions]
    settings_text = (args.settings or ROOT / "settings.yaml").read_text()
    runs = [(c, sc, s) for c in args.configs for sc in args.scenarios for s in sessions]
    sha = git_sha()
    print(f"{len(runs)} runs in {batch} (code {sha})", flush=True)
    failed = 0
    try:
        for n, (name, scenario, session) in enumerate(runs, 1):
            out = batch / name / scenario / f"s{session}"
            meta_file = out / "meta.json"
            if meta_file.exists() and json.loads(meta_file.read_text()).get("done"):
                continue
            started = time.time()
            print(f"[{n}/{len(runs)}] config {name}, {scenario}, session {session}", flush=True)
            meta = {
                "config": name,
                "about": CONFIGS[name].about,
                "scenario": scenario,
                "session": session,
                "code": sha,
                "started": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
                "settings": settings_text,
            }
            try:
                run_one(out, CONFIGS[name], scenario, session, args.settings)
                meta["done"] = True
            except (RuntimeError, subprocess.SubprocessError, OSError) as e:
                failed += 1
                meta["error"] = str(e)
                print(f"  failed: {e}", flush=True)
            meta["duration_s"] = round(time.time() - started, 1)
            meta_file.write_text(json.dumps(meta, indent=1) + "\n")
    finally:
        subprocess.run([str(NETNS), "down"], capture_output=True, check=False)
        owner = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
        if all(owner) and batch.exists():  # hand the results back to the user who ran sudo
            for path in [batch, *batch.rglob("*")]:
                shutil.chown(path, int(owner[0]), int(owner[1]))
    print(f"done: {len(runs) - failed} ok, {failed} failed", flush=True)


if __name__ == "__main__":
    main()
