"""
(Re)build the ChromaDB vector store from parsed contract documents.

Delegates entirely to src.build_vector_store — all logic lives there.

Usage:
    python scripts/build_vector_store.py       # equivalent to --build
    python -m src.build_vector_store --build   # preferred (supports --query too)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.build_vector_store import main

if __name__ == "__main__":
    sys.argv = [sys.argv[0], "--build"]
    main()
