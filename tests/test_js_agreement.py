"""The normaliser must produce the same string in a browser as it does here.

If it does not, every offset this extractor stores is unverifiable by the
frontend that has to resolve it. This runs the documented JavaScript equivalent
under node over the same inputs and requires byte-identical output.

Skipped when node is unavailable; the contract is still documented in
``normalize.py`` and README.md.
"""

import json
import random
import shutil
import subprocess

import pytest

from causalia_extractor.normalize import WHITESPACE_CHARS, WHITESPACE_CODEPOINTS, collapse

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is not installed")

# Built from the codepoints rather than written as literals, so this file holds
# no invisible characters. It is the class README.md documents.
SCRIPT = """
const data = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));
const cls = data.codes.map(c => '\\\\u' + c.toString(16).padStart(4, '0')).join('');
const WS = new RegExp('[' + cls + ']+', 'gu');
const normalize = s => s.replace(WS, ' ').trim();
console.log(JSON.stringify(data.cases.map(normalize)));
"""


def js_normalize(tmp_path, cases):
    payload = tmp_path / "cases.json"
    payload.write_text(json.dumps(
        {"codes": list(WHITESPACE_CODEPOINTS), "cases": cases}), encoding="utf-8")
    script = tmp_path / "normalize.js"
    script.write_text(SCRIPT, encoding="utf-8")
    out = subprocess.run([node, str(script), str(payload)],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_javascript_and_python_agree_on_every_whitespace_character(tmp_path):
    cases = [f"a{char}b" for char in WHITESPACE_CHARS]
    assert js_normalize(tmp_path, cases) == [collapse(c) for c in cases]


def test_javascript_and_python_agree_on_randomised_text(tmp_path):
    random.seed(7)
    pieces = list(WHITESPACE_CHARS)
    words = ["a", "Trump", "Lee-filmben", chr(0x200B), chr(0xE9), "1:45"]
    cases = []
    for _ in range(300):
        parts = []
        for _ in range(random.randint(1, 6)):
            parts.append(random.choice(words))
            parts.append("".join(random.choice(pieces)
                                 for _ in range(random.randint(0, 3))))
        cases.append("".join(parts))
    cases += ["", "   ", "a b", "a" + chr(0xA0) + "b",
              chr(0xFEFF) + "x" + chr(0x85) + "y"]
    assert js_normalize(tmp_path, cases) == [collapse(c) for c in cases]


def test_a_zero_width_space_survives_in_both(tmp_path):
    # Browsers do not collapse U+200B, so neither may we.
    case = "zero" + chr(0x200B) + "width"
    assert js_normalize(tmp_path, [case]) == [collapse(case)] == [case]
