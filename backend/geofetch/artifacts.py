"""Publish only this task's products directly into the selected directory."""
import json
from pathlib import Path
import shutil

def publish(source, destination, dataset):
    source, destination = Path(source), Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    paths = sorted(path for path in source.rglob('*') if path.is_file() and not path.is_symlink() and not path.name.endswith('.part'))
    if dataset == 'events':
        paths = [path for path in paths if path.suffix.lower() == '.pdf' or path.name == 'manifest.json']
    groups = {}
    for path in paths:
        # Keep a raster and its QGIS style under the same basename.
        group = (path.parent, path.stem)
        groups.setdefault(group, []).append(path)
    reserved = set()
    names = {}
    for group in groups.values():
        suffix = 1
        while True:
            candidates = {path: path.name if suffix == 1 else f'{path.stem}_{suffix}{path.suffix}' for path in group}
            if all(name not in reserved and not (destination/name).exists() for name in candidates.values()):
                break
            suffix += 1
        names.update(candidates)
        reserved.update(candidates.values())
    references = {path.relative_to(source).as_posix(): name for path, name in names.items()}
    def rewrite(value):
        if isinstance(value, str):
            return references.get(value, value)
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if isinstance(value, dict):
            return {key: rewrite(item) for key,item in value.items()}
        return value
    products = []
    for path, name in names.items():
        target = destination/name
        try:
            # Exclusive creation also protects existing files changed externally.
            with target.open('xb') as output:
                if dataset == 'events' and path.name == 'manifest.json':
                    output.write(json.dumps(rewrite(json.loads(path.read_text(encoding='utf-8'))), ensure_ascii=False, indent=2).encode('utf-8'))
                else:
                    with path.open('rb') as input_file:
                        shutil.copyfileobj(input_file, output, length=1024*1024)
        except FileExistsError:
            raise
        except Exception:
            target.unlink(missing_ok=True)
            raise
        products.append(name)
    return products
