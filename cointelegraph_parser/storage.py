"""Интерфейс хранилища: contains(url) и save(article) -> bool."""

import csv
import json
import os
from pathlib import Path

from source import article_id

FIELDS = ["id", "language", "url", "title", "text", "published_at",
          "tags", "symbols", "first_seen_at"]


class CsvStorage:
    def __init__(self, path):
        self.path = Path(path)
        self.ids = set()
        # Полный текст может быть длиннее стандартного лимита csv в 128 КиБ.
        csv.field_size_limit(10 * 1024 * 1024)
        if self.path.exists() and self.path.stat().st_size:
            with self.path.open(encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                if reader.fieldnames != FIELDS:
                    raise ValueError("Unexpected CSV columns")
                for row in reader:
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError("Incomplete CSV row; repair the file before continuing")
                    if row["id"] != article_id(row["url"]) or row["id"] in self.ids:
                        raise ValueError("Invalid or duplicate CSV ID")
                    self.ids.add(row["id"])

    def contains(self, url):
        return article_id(url) in self.ids

    def save(self, article):
        if self.contains(article["url"]):
            return False
        row = dict(article)
        if row["id"] != article_id(row["url"]):
            raise ValueError("Article ID does not match URL")
        for field in ("tags", "symbols"):
            row[field] = json.dumps(row[field], ensure_ascii=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        needs_header = not self.path.exists() or self.path.stat().st_size == 0
        with self.path.open("a", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            if needs_header:
                writer.writeheader()
            writer.writerow(row)
            stream.flush()
            os.fsync(stream.fileno())
        self.ids.add(row["id"])
        return True
