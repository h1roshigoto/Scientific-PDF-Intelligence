"""Local JSON persistence; technical metadata and scientific records stay separate."""
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .models import ResultRecord, ValidationIssue


class Store:
    def __init__(self, root="state"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def read(self, name):
        path = self.root / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def write(self, name, data):
        with NamedTemporaryFile("w", encoding="utf-8", dir=self.root, delete=False) as temp:
            json.dump(data, temp, ensure_ascii=False, indent=2)
            temp.write("\n")
            target = temp.name
        os.replace(target, self.root / name)

    def upsert(self, name, item, key):
        data = [row for row in self.read(name) if row[key] != item[key]]
        data.append(item)
        self.write(name, data)

    def documents(self):
        return self.read("documents.json")

    def results(self, domain=None):
        return [r for r in map(ResultRecord.model_validate, self.read("results.json"))
                if domain is None or r.domain == domain]

    def issues(self, domain=None):
        return [r for r in map(ValidationIssue.model_validate, self.read("issues.json"))
                if domain is None or r.domain == domain]
