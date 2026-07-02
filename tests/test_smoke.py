
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_template_validates():
    result = subprocess.run([sys.executable, str(ROOT / "cli" / "go.py"), "validate", str(ROOT.parent / "go-project-template")], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr + result.stdout
