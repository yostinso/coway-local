"""Generate the JS and Python BLE protocol constants from protocol/ble.json.

python scripts/gen_protocol.py          # write the generated files
python scripts/gen_protocol.py --check  # fail if they are out of date (CI)
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "protocol" / "ble.json"
JS = ROOT / "web" / "v1" / "protocol-constants.mjs"
PY = ROOT / "custom_components" / "coway_local" / "ble_constants.py"
HEADER = "Generated from protocol/ble.json by scripts/gen_protocol.py. Do not edit."


def uuid(short):
    return f"0000{short}-0000-1000-8000-00805f9b34fb"


def render_js(spec):
    lines = [f"// {HEADER}"]
    for name, short in spec["uuids"].items():
        lines.append(f"export const {name} = '{uuid(short)}';")
    for name, value in {**spec["manufacturers"], **spec["codes"]}.items():
        lines.append(f"export const {name} = {value};")
    fields = ", ".join(f"{k}: '{v}'" for k, v in spec["fields"].items())
    lines.append(f"export const FIELDS = {{ {fields} }};")
    failures = ", ".join(map(str, spec["failure_codes"]))
    lines.append(f"export const FAILURE_CODES = new Set([{failures}]);")
    status = ", ".join(
        f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in spec["status"].items()
    )
    lines.append(f"export const STATUS = {{ {status} }};")
    return "\n".join(lines) + "\n"


def render_py(spec):
    lines = [f'"""{HEADER}"""', ""]
    for name, short in spec["uuids"].items():
        lines.append(f'{name} = "{uuid(short)}"')
    for name, value in {**spec["manufacturers"], **spec["codes"]}.items():
        lines.append(f"{name} = {value}")
    lines.append("FIELDS = {")
    lines += [f'    {k}: "{v}",' for k, v in spec["fields"].items()]
    lines.append("}")
    lines.append("FAILURE_CODES = frozenset(")
    lines.append("    {" + ", ".join(map(str, spec["failure_codes"])) + "}")
    lines.append(")")
    lines.append("STATUS = {")
    lines += [f"    {k}: {json.dumps(v, ensure_ascii=False)}," for k, v in spec["status"].items()]
    lines.append("}")
    return "\n".join(lines) + "\n"


def main():
    spec = json.loads(SOURCE.read_text(encoding="utf-8"))
    outputs = {JS: render_js(spec), PY: render_py(spec)}
    if "--check" in sys.argv:
        stale = [p for p, text in outputs.items() if not p.exists() or p.read_text("utf-8") != text]
        for path in stale:
            print(f"{path.relative_to(ROOT)} is out of date; run python scripts/gen_protocol.py")
        sys.exit(1 if stale else 0)
    for path, text in outputs.items():
        path.write_text(text, encoding="utf-8", newline="\n")
        print(path.relative_to(ROOT))


if __name__ == "__main__":
    main()
