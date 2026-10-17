"""Helpers for workflow transition knowledge-article payloads.

Workflow knowledge articles live in JSONField columns but are also writable via
MCP tools whose arguments are JSON strings. Keep all read/write paths on one
contract so malformed legacy rows cannot crash transition rendering.
"""

from __future__ import annotations

import json
from typing import Any


_ALLOWED_TYPES = {"inline", "wiki", "link"}


def normalize_knowledge_articles(value: Any, *, reject_malformed: bool = False) -> list[dict[str, str]]:
    """Return a safe list of knowledge-article dicts.

    Read paths call this with ``reject_malformed=False``: malformed legacy values
    are skipped/coerced so a single bad JSONField value never breaks transition
    listing or execution responses.

    Write paths call it with ``reject_malformed=True``: JSON-string payloads are
    parsed, but malformed shapes are rejected instead of being persisted.
    """

    def malformed(message: str) -> list[dict[str, str]]:
        if reject_malformed:
            raise ValueError(message)
        return []

    if value is None or value == "":
        return []

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return malformed("knowledge_articles must be a JSON array of article objects")

    if isinstance(value, dict):
        value = [value]

    if not isinstance(value, list):
        return malformed("knowledge_articles must be a JSON array of article objects")

    articles: list[dict[str, str]] = []
    for index, article in enumerate(value):
        if isinstance(article, str):
            try:
                article = json.loads(article)
            except json.JSONDecodeError:
                if reject_malformed:
                    raise ValueError(f"knowledge_articles[{index}] must be an object, not a string")
                continue

        if not isinstance(article, dict):
            if reject_malformed:
                raise ValueError(f"knowledge_articles[{index}] must be an object")
            continue

        article_type = article.get("type") or "inline"
        if article_type not in _ALLOWED_TYPES:
            if reject_malformed:
                raise ValueError(
                    f"knowledge_articles[{index}].type must be one of: "
                    f"{', '.join(sorted(_ALLOWED_TYPES))}"
                )
            article_type = "inline"

        normalized = {
            "type": str(article_type),
            "title": str(article.get("title") or "").strip(),
        }
        if article_type == "inline":
            normalized["content"] = str(article.get("content") or "")
        else:
            normalized["url"] = str(article.get("url") or "")

        articles.append(normalized)

    return articles
