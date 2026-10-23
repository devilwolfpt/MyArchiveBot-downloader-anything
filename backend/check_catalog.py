import sys
sys.path.insert(0, "backend")

from app.core.extractors.catalog import SiteCatalogExtractor
inst = SiteCatalogExtractor()
classes = inst._get_classes()
print(f"Classes carregadas do sites/extractor/: {len(classes)}")

# Quais ficheiros físicos existem em sites/extractor/?
from pathlib import Path
extractor_dir = Path("backend/app/core/extractors/sites/extractor")
py_files = list(extractor_dir.rglob("*.py"))
print(f"Ficheiros .py em sites/extractor/: {len(py_files)}")
print(f"Subpastas em sites/extractor/: {[d.name for d in extractor_dir.iterdir() if d.is_dir()]}")
