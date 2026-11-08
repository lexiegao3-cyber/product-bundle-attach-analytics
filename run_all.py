"""Raw Excel -> baskets -> association rules, RFM, Excel Bundle Explorer, findings."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for step in ["src/etl.py", "src/analysis.py"]:
    print(f"\n=== {step}")
    subprocess.run([sys.executable, str(ROOT / step)], check=True)
print("\nDone: reports/findings.md and excel/bundle_explorer.xlsx")
