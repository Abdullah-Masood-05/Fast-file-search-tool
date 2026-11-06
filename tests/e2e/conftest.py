import sys
from pathlib import Path

# helpers live next to the tests; make them importable regardless of the rootdir mode
sys.path.insert(0, str(Path(__file__).resolve().parent))
