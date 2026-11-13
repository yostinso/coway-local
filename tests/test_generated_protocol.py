"""The generated constants files must match protocol/ble.json."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gen_protocol", ROOT / "scripts" / "gen_protocol.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


@pytest.mark.parametrize(
    ("path", "render"), [(gen.JS, gen.render_js), (gen.PY, gen.render_py)], ids=["js", "py"]
)
def test_generated_file_is_current(path, render):
    expected = render(json.loads(gen.SOURCE.read_text(encoding="utf-8")))
    assert path.read_text(encoding="utf-8") == expected, (
        f"{path.relative_to(ROOT)} is out of date; run python scripts/gen_protocol.py"
    )
