from dataclasses import fields

import pytest
import yaml

from edgeproxy.common.settings import SECTIONS, SETTINGS_FILE, Settings, load_settings


def test_settings_file_loads_and_lists_every_setting():
    """settings.yaml is the place to look for knobs, so it must mention all of them."""
    load_settings()
    raw = yaml.safe_load(SETTINGS_FILE.read_text())
    for name, cls in SECTIONS.items():
        assert set(raw[name]) == {f.name for f in fields(cls)}, name


def test_missing_file_or_keys_use_defaults(tmp_path):
    assert load_settings(tmp_path / "missing.yaml") == Settings()
    f = tmp_path / "s.yaml"
    f.write_text("jev:\n  max_options: 100\nclient:\n  revalidate_timeout_s: 3\n")
    s = load_settings(f)
    assert s.jev.max_options == 100
    assert s.client.revalidate_timeout_s == 3.0 and isinstance(s.client.revalidate_timeout_s, float)
    assert s.prefetch == Settings().prefetch


@pytest.mark.parametrize(
    "text, error, message",
    [
        ("jev:\n  max_option: 100\n", ValueError, "unknown key.*max_option"),
        ("jevv:\n  max_options: 100\n", ValueError, "unknown section.*jevv"),
        ("jev:\n  max_options: lots\n", TypeError, "jev.max_options: expected int"),
        ("links:\n  same_origin_only: 1\n", TypeError, "expected bool"),
        ("client:\n  cache_mb: true\n", TypeError, "expected int"),
    ],
)
def test_mistakes_are_errors(tmp_path, text, error, message):
    f = tmp_path / "s.yaml"
    f.write_text(text)
    with pytest.raises(error, match=message):
        load_settings(f)
