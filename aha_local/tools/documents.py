# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The household's documents folder: search (BM25 over text chunks) and read (SPEC §3.4).

The folder is mounted read-only. The index is rebuilt when any file changes; it lives in memory only.
"""

from __future__ import annotations

import csv
import io
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .registry import Registry, Tool, ToolContext, ToolResult, params, string

log = logging.getLogger("aha.documents")

TEXT_SUFFIXES = {".md", ".txt", ".csv", ".tsv", ".json", ".log"}
SUFFIXES = TEXT_SUFFIXES | {".pdf", ".docx"}
CHUNK_CHARS = 900
MAX_FILE_BYTES = 20_000_000
MAX_READ_CHARS = 20_000
WORD = re.compile(r"[\w€£$%-]+", re.UNICODE)


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        raw = path.read_text(errors="replace")
        if suffix in (".csv", ".tsv"):
            rows = list(csv.reader(io.StringIO(raw), delimiter="\t" if suffix == ".tsv" else ","))
            return "\n".join(" | ".join(r) for r in rows)
        return raw
    if suffix == ".pdf":
        from pypdf import PdfReader

        return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    if suffix == ".docx":
        import docx

        return "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
    return ""


def tokens(text: str) -> list[str]:
    return [w.lower() for w in WORD.findall(text)]


@dataclass
class Chunk:
    path: str
    text: str
    terms: Counter


class Index:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._signature: tuple = ()
        self.chunks: list[Chunk] = []
        self.df: Counter = Counter()

    def files(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(
            p
            for p in self.root.rglob("*")
            if p.is_file()
            and p.suffix.lower() in SUFFIXES
            and not p.name.startswith(".")
            and p.stat().st_size <= MAX_FILE_BYTES
        )

    def refresh(self) -> None:
        files = self.files()
        signature = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
        if signature == self._signature:
            return
        chunks: list[Chunk] = []
        for p in files:
            try:
                text = extract_text(p)
            except Exception as e:
                log.warning("couldn't read a document (%s)", type(e).__name__)
                continue
            rel = str(p.relative_to(self.root))
            paragraphs = [x.strip() for x in re.split(r"\n\s*\n", text) if x.strip()]
            buf = ""
            for para in paragraphs or [text]:
                if buf and len(buf) + len(para) > CHUNK_CHARS:
                    chunks.append(Chunk(rel, buf, Counter(tokens(rel + " " + buf))))
                    buf = ""
                buf = f"{buf}\n\n{para}".strip()
            if buf:
                chunks.append(Chunk(rel, buf, Counter(tokens(rel + " " + buf))))
        self.chunks, self._signature = chunks, signature
        self.df = Counter(t for c in chunks for t in c.terms)

    def search(self, query: str, k: int = 6) -> list[tuple[float, Chunk]]:
        self.refresh()
        q = set(tokens(query))
        if not q or not self.chunks:
            return []
        n = len(self.chunks)
        avg = sum(sum(c.terms.values()) for c in self.chunks) / n
        scored = []
        for c in self.chunks:
            length = sum(c.terms.values())
            score = 0.0
            for t in q:
                tf = c.terms.get(t, 0)
                if not tf:
                    continue
                idf = math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / avg))
            if score > 0:
                scored.append((score, c))
        return sorted(scored, key=lambda x: -x[0])[:k]


_indexes: dict[Path, Index] = {}


def index_for(ctx: ToolContext) -> Index:
    root = ctx.config.documents_mount
    return _indexes.setdefault(root, Index(root))


async def documents_search(args: dict, ctx: ToolContext) -> ToolResult:
    query = str(args.get("query", ""))
    hits = index_for(ctx).search(query)
    if not hits:
        files = index_for(ctx).files()
        return ToolResult(f"No document matches “{query}”. The folder has {len(files)} file(s).")
    parts = [f"[{c.path}]\n{c.text[:CHUNK_CHARS]}" for _, c in hits]
    return ToolResult("Matching passages (name the file you use):\n\n" + "\n\n---\n\n".join(parts))


async def documents_read(args: dict, ctx: ToolContext) -> ToolResult:
    root = ctx.config.documents_mount.resolve()
    target = (root / str(args.get("path", ""))).resolve()
    if not target.is_relative_to(root) or not target.is_file() or target.suffix.lower() not in SUFFIXES:
        return ToolResult("There's no such document. Use documents_search to find file names.", error=True)
    text = extract_text(target)
    cut = "\n[… cut to fit]" if len(text) > MAX_READ_CHARS else ""
    return ToolResult(f"[{target.relative_to(root)}]\n{text[:MAX_READ_CHARS]}{cut}")


def register(reg: Registry) -> None:
    reg.add(
        Tool(
            "documents_search",
            "Search the household's own documents (bills, warranties, travel plans, "
            "records). Returns file names and matching passages.",
            params(["query"], query=string("Keywords, e.g. 'washing machine warranty'")),
            documents_search,
        )
    )
    reg.add(
        Tool(
            "documents_read",
            "Read one of the household's documents in full.",
            params(["path"], path=string("File name as returned by documents_search")),
            documents_read,
        )
    )
