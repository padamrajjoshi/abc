"""Restore executable script permissions missing from the NBIS mirror."""
from pathlib import Path

for path in Path('.').rglob('*'):
    if '.git' in path.parts or not path.is_file():
        continue
    with path.open('rb') as source:
        is_script = source.read(2) == b'#!'
    if is_script:
        path.chmod(path.stat().st_mode | 0o111)
