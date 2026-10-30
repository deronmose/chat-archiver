"""JSON, CSV and HTML export of an archive.

Exported text comes from other people and is untrusted:
- CSV cells that Excel would treat as formulas are prefixed with an apostrophe;
- HTML is fully escaped and the page forbids scripts via Content-Security-Policy.
"""
from __future__ import annotations

import csv
import html
import json
import os
from pathlib import Path
from urllib.parse import quote

from tg_export.store import COLUMNS, Archive

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp")
EXPORT_COLUMNS = [c for c in COLUMNS if c != "raw"]
EXPORT_COLUMNS.insert(EXPORT_COLUMNS.index("topic_id") + 1, "topic")


def _rows_with_topic(archive: Archive):
    topics = archive.topics()
    for row in archive.rows():
        row["topic"] = topics.get(row["topic_id"]) if row.get("topic_id") is not None else None
        yield row


def _atomic_writer(path: Path, newline: str | None = None, encoding: str = "utf-8"):
    tmp = path.with_name(path.name + ".tmp")
    return tmp, open(tmp, "w", encoding=encoding, newline=newline)


def csv_safe(value):
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value


def export_json(archive: Archive, path: Path) -> int:
    tmp, f = _atomic_writer(path)
    n = 0
    with f:
        f.write("[\n")
        for row in _rows_with_topic(archive):
            if n:
                f.write(",\n")
            if row.get("raw"):
                row["raw"] = json.loads(row["raw"])
            else:
                row.pop("raw", None)
            json.dump(row, f, ensure_ascii=False)
            n += 1
        f.write("\n]\n")
    os.replace(tmp, path)
    return n


def export_csv(archive: Archive, path: Path) -> int:
    # utf-8-sig so Excel opens Cyrillic correctly
    tmp, f = _atomic_writer(path, newline="", encoding="utf-8-sig")
    n = 0
    with f:
        w = csv.writer(f)
        w.writerow(EXPORT_COLUMNS)
        for row in _rows_with_topic(archive):
            w.writerow([csv_safe(row[c]) for c in EXPORT_COLUMNS])
            n += 1
    os.replace(tmp, path)
    return n


_PAGE_HEAD = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self' file:; media-src 'self' file:; style-src 'unsafe-inline'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body{{font:15px/1.45 system-ui,sans-serif;max-width:860px;margin:0 auto;padding:16px;background:#f4f4f5;color:#18181b}}
h1{{font-size:20px}} .meta{{color:#71717a;font-size:13px}}
.m{{background:#fff;border-radius:10px;padding:10px 14px;margin:8px 0;box-shadow:0 1px 2px #0001}}
.m.svc{{background:none;box-shadow:none;text-align:center;color:#71717a;font-size:13px}}
.h{{font-size:13px;color:#52525b;margin-bottom:4px}} .h b{{color:#1d4ed8}}
.t{{white-space:pre-wrap;word-wrap:break-word}} .r{{font-size:12px;color:#71717a}}
.tp{{background:#e0e7ff;color:#3730a3;border-radius:6px;padding:0 6px;font-size:12px}}
img{{max-width:100%;max-height:480px;border-radius:6px;display:block;margin-top:6px}}
a{{color:#1d4ed8}}
@media (prefers-color-scheme:dark){{body{{background:#18181b;color:#e4e4e7}}.m{{background:#27272a}}.h{{color:#a1a1aa}}.h b,a{{color:#93c5fd}}}}
</style></head><body>
<h1>{title}</h1><p class="meta">{subtitle}</p>
"""


def export_html(archive: Archive, path: Path) -> int:
    meta = archive.meta()
    title = html.escape(meta.get("title") or "Чат")
    subtitle = html.escape(f"ID {meta.get('chat_id', '?')} · выгружено {meta.get('exported_at', '')}")
    tmp, f = _atomic_writer(path)
    n = 0
    with f:
        f.write(_PAGE_HEAD.format(title=title, subtitle=subtitle))
        show_topic = len(archive.topics()) > 1
        for row in _rows_with_topic(archive):
            n += 1
            mid = int(row["id"])
            if row["service"]:
                f.write(f'<div class="m svc" id="m{mid}">{html.escape(row["date"][:16].replace("T", " "))} · '
                        f'{html.escape(row["service"])}</div>\n')
                continue
            who = html.escape(row["sender_name"] or str(row["sender_id"] or ""))
            topic = f' · <span class="tp">{html.escape(row["topic"])}</span>' if show_topic and row["topic"] else ""
            parts = [f'<div class="m" id="m{mid}"><div class="h"><b>{who}</b> · '
                     f'{html.escape(row["date"][:16].replace("T", " "))} · #{mid}{topic}</div>']
            if row["reply_to"]:
                parts.append(f'<div class="r">↪ <a href="#m{int(row["reply_to"])}">ответ на #{int(row["reply_to"])}</a></div>')
            if row["fwd_from"]:
                parts.append(f'<div class="r">переслано от {html.escape(row["fwd_from"])}</div>')
            if row["text"]:
                parts.append(f'<div class="t">{html.escape(row["text"])}</div>')
            if row["media_path"]:
                href = quote(row["media_path"].replace("\\", "/"))
                if row["media_path"].lower().endswith(_IMAGE_EXT):
                    parts.append(f'<img src="{href}" loading="lazy" alt="">')
                else:
                    parts.append(f'<a href="{href}">📎 {html.escape(row["media_name"] or row["media_path"])}</a>')
            elif row["media_type"]:
                parts.append(f'<div class="r">[{html.escape(row["media_type"])}'
                             + (f': {html.escape(row["media_name"])}' if row["media_name"] else "") + "]</div>")
            parts.append("</div>\n")
            f.write("".join(parts))
        f.write("</body></html>\n")
    os.replace(tmp, path)
    return n


EXPORTERS = {
    "json": ("messages.json", export_json),
    "csv": ("messages.csv", export_csv),
    "html": ("chat.html", export_html),
}


def export_all(archive: Archive, folder: Path, formats: list[str]) -> list[tuple[Path, int]]:
    done = []
    for fmt in formats:
        filename, fn = EXPORTERS[fmt]
        target = folder / filename
        done.append((target, fn(archive, target)))
    return done
