"""CI gate: every tool registered in __init__.py is listed in plugin.yaml's provides_tools, and vice versa."""

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
registered = set(re.findall(r'\("(pr_\w+)", schemas\.', (ROOT / "__init__.py").read_text()))
declared = set(manifest["provides_tools"])
if manifest["name"] != "pr-sentinel" or registered != declared:
    sys.exit(f"manifest mismatch: registered={sorted(registered)} declared={sorted(declared)}")
print(f"ok: {len(declared)} tools")
