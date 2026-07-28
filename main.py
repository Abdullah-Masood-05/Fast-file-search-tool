import sys
import os

# Insert src directory to path for local execution support
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from fast_file_search.main import main

if __name__ == "__main__":
    main()