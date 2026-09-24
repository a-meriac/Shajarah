"""Real interface switch in the netns topology. Linux only: `make test-netns`."""

import json
import os
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.netns
PY = sys.executable


def _ns(ns, *args, **kw):
    return subprocess.Popen(
        ["ip", "netns", "exec", ns, PY, "-m", "experiments.tunnel_probe", *args],
        stdout=subprocess.PIPE,
        text=True,
        **kw,
    )


@pytest.fixture
def topology():
    if os.geteuid() != 0:
        pytest.skip("needs root")
    subprocess.run(["emulation/netns_setup.sh", "up"], check=True)
    server = _ns("ep-srv", "server", "--certdir", "/tmp/ep-test")
    time.sleep(1.0)
    yield
    server.terminate()
    subprocess.run(["emulation/netns_setup.sh", "down"], check=True)


def _client(mode):
    p = _ns("ep-cli", "client", "--certdir", "/tmp/ep-test", "--mode", mode)
    out, _ = p.communicate(timeout=30)
    return json.loads(out.strip().splitlines()[-1])


def test_migration_survives_wifi_loss(topology):
    r = _client("migrate")
    assert r["completed_after_switch"] > 20
    assert r["max_gap_s"] < 1.0


def test_without_migration_the_tunnel_stalls(topology):
    r = _client("none")
    assert r["completed_after_switch"] == 0
