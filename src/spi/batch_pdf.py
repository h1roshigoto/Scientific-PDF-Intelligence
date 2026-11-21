"""PyMuPDF evidence fragments for the batch extractor."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from itertools import groupby
from statistics import median

import pymupdf

from .gigachat_evidence import Block, _NUMBER


CAPTION = re.compile(r"^\s*((?:Table|Fig(?:ure)?\.?)\s+[A-Z]?\d+[a-z]?)\s*(?:[.:|–—]|$)", re.I)
CELL = re.compile(rf"({_NUMBER})(?:\s*(±\s*{_NUMBER}))?(?:\s*(%|[A-Za-zµμΩΩ][\wµμ²^/ ()⁻−-]*))?")
RECORD = re.compile(rf"\(({_NUMBER})\)")
LAYOUT_VERSION = f"batch-layout-9/pymupdf-{pymupdf.VersionBind}"
UNIT_ONLY = re.compile(r"^\([^()]+\)\s*[↑↓]?$")
SCAN_AXES = {"scan direction", "sweep direction"}
SCAN_DIRECTIONS = {"forward", "reverse", "average"}


@dataclass(frozen=True)
class Cell:
    value: Block
    row: tuple[Block, ...]
    headers: tuple[Block, ...]
    aggregation: str | None = None
    row_axis: Block | None = None


@dataclass(frozen=True)
class Fragment:
    id: str
    page: int
    blocks: tuple[Block, ...]
    cells: tuple[Cell, ...] = ()
    caption: Block | None = None
    location: str | None = None
    caption_support: tuple[Block, ...] = ()
    table_references: tuple[Block, ...] = ()
    metric_scale_support: tuple[Block, ...] = ()
    caption_has_reference: bool = False

    def payload(self):
        return {
            "pdf_page": self.page, "location": self.location,
            "blocks": [asdict(block) for block in self.blocks],
            "table_references": [b.block_id for b in self.table_references],
            "metric_scale_support": [b.block_id for b in self.metric_scale_support],
            "table_cells": [
                {"cell": cell.value.block_id, "row": [b.block_id for b in cell.row],
                 "headers": [b.block_id for b in cell.headers], "aggregation": cell.aggregation,
                 "row_axis": cell.row_axis.block_id if cell.row_axis else None}
                for cell in self.cells
            ],
        }


def center(block: Block) -> float:
    return (block.bbox[0] + block.bbox[2]) / 2


def _lines(page, numeric_superscripts=None):
    raw = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
    for obj in raw["blocks"]:
        for i, line in enumerate(obj.get("lines", ())):
            spans = line["spans"]
            runs = []
            for span in spans:
                if numeric_superscripts is not None and span["flags"] & pymupdf.TEXT_FONT_SUPERSCRIPT and (
                    re.fullmatch(r"\d+(?:[,–-]\d+)*", span["text"].strip())
                ):
                    numeric_superscripts.append(span)
                # PyMuPDF can emit a standalone space bridging two adjacent columns.
                if not span["text"].strip() and span["bbox"][2] - span["bbox"][0] > max(3, span["size"] * .5):
                    continue
                if runs and span["bbox"][0] - runs[-1][-1]["bbox"][2] <= max(3, span["size"] * .5):
                    runs[-1].append(span)
                else:
                    runs.append([span])
            for k, run in enumerate(runs):
                text = "".join(span["text"] for span in run).strip()
                if text:
                    rect = pymupdf.Rect(run[0]["bbox"])
                    for span in run[1:]:
                        rect |= pymupdf.Rect(span["bbox"])
                    yield (Block(f"p{page.number + 1}-b{obj['number']}-l{i}-s{k}", tuple(rect), text),
                           round(median(span["origin"][1] for span in run), 1))


def _number(block):
    return CELL.fullmatch(block.text) is not None


def _bands(lines):
    rows = []
    for block, baseline in sorted(lines, key=lambda pair: pair[1]):
        if not rows or abs(rows[-1][0] - baseline) > 2.5:
            rows.append((baseline, []))
        rows[-1][1].append(block)
    return [(y, sorted(row, key=center)) for y, row in rows]


def _caption_location(block):
    return CAPTION.match(block.text)[1].replace(".", "")


def _ruled(page, caption, tables):
    nearby = []
    for table in tables:
        rect = pymupdf.Rect(table.bbox)
        cy = caption.bbox[1]
        gap = min(abs(cy - rect.y0), abs(cy - rect.y1))
        if gap < 64 and rect.x0 - 15 <= caption.bbox[0] <= rect.x1 and table.row_count >= 2:
            nearby.append((gap, table))
    if len(nearby) != 1:
        return []
    table = nearby[0][1]
    values = table.extract()
    rows = []
    for i, (row, texts) in enumerate(zip(table.rows, values, strict=True)):
        rows.append([
            Block(f"{caption.block_id}-r{i}c{j}", tuple(rect),
                  page.get_textbox(pymupdf.Rect(rect)).strip())
            if rect and text and page.get_textbox(pymupdf.Rect(rect)).strip() else None
            for j, (rect, text) in enumerate(zip(row.cells, texts, strict=True))
        ])
    start = next((i for i, row in enumerate(rows) if row[0] and any(b and _number(b) for b in row[1:])), None)
    if not start or start > 5:
        return []
    cells = []
    for row in rows[start:]:
        if not row[0] or _number(row[0]):
            continue
        for value in row[1:]:
            if value is None or not _number(value):
                continue
            headers = []
            for header_row in rows[:start]:
                matches = [block for block in header_row if block and block.bbox[0] <= center(value) <= block.bbox[2]]
                if len(matches) != 1:
                    break
                headers.append(matches[0])
            if len(headers) == start:
                cells.append(Cell(value, (row[0],), tuple(dict.fromkeys(headers)), row_axis=rows[start - 1][0]))
    return cells


def _aligned_groups(labels, leaves):
    if len(labels) < 2 or len(leaves) % len(labels):
        return None
    size = len(leaves) // len(labels)
    pitch = median(b - a for a, b in zip(leaves, leaves[1:]))
    groups = [label for label in labels for _ in range(size)]
    for i, label in enumerate(labels):
        group = leaves[i*size:(i+1)*size]
        if abs(center(label) - (group[0] + group[-1]) / 2) > pitch * .65:
            return None
    return groups


def _scan_rows(bands, caption):
    headings = [(i, row) for i, (y, row) in enumerate(bands)
                if 0 < y - caption.bbox[1] < 80 and len(row) >= 4 and row[1].text.casefold() in SCAN_AXES]
    if len(headings) != 1:
        return []
    index, header = headings[0]
    cells = []
    device = None
    previous_direction = None
    prior_y = bands[index][0]
    for y, row in bands[index + 1:]:
        if y - prior_y > 24:
            break
        direction = next((b for b in row if b.text.casefold() in SCAN_DIRECTIONS
                          and header[1].bbox[0] - 5 <= center(b) <= header[1].bbox[2] + 5), None)
        if direction is None:
            break
        prefix = row[:row.index(direction)]
        if len(prefix) == 1 and prefix[0].bbox[2] < header[1].bbox[0]:
            device = prefix[0]
        elif prefix or previous_direction != "forward" or direction.text.casefold() != "reverse":
            break
        values = row[row.index(direction) + 1:]
        if not device or not values or not all(map(_number, values)):
            break
        matches = [[h for h in header[2:] if h.bbox[0] - 6 <= center(b) <= h.bbox[2] + 6] for b in values]
        if any(len(link) != 1 for link in matches):
            break
        labels = [link[0] for link in matches]
        if len(set(labels)) != len(values):
            break
        cells.extend(Cell(value, (device, direction), (label,), row_axis=header[1])
                     for value, label in zip(values, labels, strict=True))
        prior_y, previous_direction = y, direction.text.casefold()
    return cells if len({cell.row for cell in cells}) >= 2 else []


def _borderless(caption, lines, support):
    # Look for a repeated, independently aligned grid within the caption's column(s).
    cx, cy, _, _ = caption.bbox
    end = max(b.bbox[2] for b in support)
    bands = _bands((b, y) for b, y in lines
                   if abs(y - cy) < 245 and b.bbox[0] >= cx - 24 and b.bbox[2] < end + 41)
    scan_bands = _bands((b, y) for b, y in lines
                        if cy < y < cy + 185 and b.bbox[0] >= cx - 24)
    if cells := _scan_rows(scan_bands, caption):
        return cells
    grids = []
    for i, (y, row) in enumerate(bands):
        usable = [row[0], *(b for b in row[1:] if _number(b))]
        if not 3 <= len(usable) <= 31 or not re.search(r"[A-Za-z]|^\d+(?:\.\d+)?%$", usable[0].text):
            continue
        columns = [center(b) for b in usable[1:]]
        if len(columns) < 2 or min(b - a for a, b in zip(columns, columns[1:])) < 7:
            continue
        if grids and abs(y - grids[-1][-1][1]) <= 33 and len(columns) == len(grids[-1][0][3]) and all(
            abs(a - b) <= 5 for a, b in zip(columns, grids[-1][0][3])
        ):
            grids[-1].append((i, y, usable, columns))
        else:
            grids.append([(i, y, usable, columns)])
    other_captions = [b.bbox[1] for b, _ in lines if b != caption and b.bbox[0] >= cx - 24
                      and b.bbox[2] < end + 41 and CAPTION.match(b.text)
                      and b.text.lower().startswith("table")]
    grids = [
        grid for grid in grids if len(grid) >= 2 and (
            0 < grid[0][1] - cy < 105 and not any(cy < y < grid[0][1] for y in other_captions)
            or 0 < cy - grid[-1][1] < 52 and not any(grid[0][1] - 85 < y < cy for y in other_captions)
        )
    ]
    if len(grids) != 1:
        return []
    grid = grids[0]
    first_band, first_y, first_row, columns = grid[0]
    levels = []
    leaf_candidates = []
    row_axis = None
    pitch = median(b - a for a, b in zip(columns, columns[1:]))
    for y, row in reversed(bands[:first_band]):
        if first_y - y > (34 if not levels else 23):
            break
        labels = [b for b in row if center(b) >= columns[0] - pitch * .48]
        if caption in labels:
            break
        if not labels:
            continue
        if not levels and len(labels) == 1:
            continue
        if not levels and len(labels) == len(columns) and all(
            abs(center(label) - center(value)) <= min(pitch * .43, 8)
            or abs(label.bbox[0] - value.bbox[0]) <= 4
            for label, value in zip(labels, first_row[1:], strict=True)
        ):
            leaf_candidates = labels
            extra = [b for b in row if b not in labels]
            row_axis = extra[0] if len(extra) == 1 else None
            levels.append(labels)
            first_y = y
        elif levels:
            # A single metric may sit immediately above an otherwise unit-only leaf.
            if len(labels) == 1:
                candidates = [j for j, leaf in enumerate(leaf_candidates)
                              if UNIT_ONLY.fullmatch(leaf.text) and (
                                  abs(center(labels[0]) - center(leaf)) <= 8
                                  or abs(labels[0].bbox[0] - leaf.bbox[0]) <= 4)]
                if len(candidates) == 1:
                    level = [None] * len(columns)
                    level[candidates[0]] = labels[0]
                    levels.insert(0, level)
                break
            expanded = _aligned_groups(labels, columns)
            if expanded is None:
                # An unresolved header spanning the value columns invalidates the path.
                if len(labels) > 1:
                    return []
                break
            levels.insert(0, expanded)
            first_y = y
    if not leaf_candidates:
        return []
    if any(_number(row[0]) for _, _, row, _ in grid) and (
        row_axis is None or abs(row_axis.bbox[0] - first_row[0].bbox[0]) > 8
        or any(abs(row[0].bbox[0] - first_row[0].bbox[0]) > 8 for _, _, row, _ in grid)
    ):
        return []
    cells = []
    group = None
    prior_band = max(-1, first_band - 2)
    for row_band, _, row, _ in grid:
        for _, intermediate in bands[max(0, prior_band + 1):row_band]:
            if len(intermediate) == 1 and not _number(intermediate[0]) and center(intermediate[0]) > columns[0] + pitch:
                group = intermediate[0]
        row_path = (group, row[0]) if group else (row[0],)
        cells.extend(Cell(cell, row_path, tuple(level[j] for level in levels if level[j]), row_axis=row_axis)
                     for j, cell in enumerate(row[1:]))
        prior_band = row_band
    return cells


def _caption_blocks(caption, lines):
    stem = caption.block_id.rsplit("-l", 1)[0]
    support = tuple(b for b, _ in lines if b.block_id.startswith(stem + "-l")
                    and 0 <= b.bbox[1] - caption.bbox[1] < 30)
    if len(support) == 1 and re.fullmatch(CAPTION, caption.text):
        inline = [b for b, _ in lines if abs(b.bbox[1] - caption.bbox[1]) <= 2
                  and 0 < b.bbox[0] - caption.bbox[2] <= 12 and not CAPTION.match(b.text)]
        if len(inline) == 1:
            support += tuple(inline)
    return support


def _mean_record(caption, lines):
    # Detect the split numerical rows only when the caption describes their notation.
    bands = _bands((b, y) for b, y in lines if caption.bbox[1] < y < caption.bbox[1] + 340)
    candidates = [i for i, (_, row) in enumerate(bands) if any(b.text.lower() == "sample" for b in row)]
    if len(candidates) != 1:
        return []
    index = candidates[0]
    if index == 0 or index + 1 >= len(bands):
        return []
    label = next(b for b in bands[index][1] if b.text.lower() == "sample")
    y, headings = bands[index - 1]
    unit_y, units = bands[index + 1]
    if unit_y - y > 24:
        return []
    headings = [(h, u) for h in headings for u in units if abs(center(h) - center(u)) <= 4 and RECORD.fullmatch(u.text) is None
                and re.fullmatch(r"\([^()]+\)", u.text)]
    if len(headings) < 3:
        return []
    cells = []
    for i in range(index + 3, len(bands) - 1):
        row_y, row = bands[i]
        before_y, before = bands[i - 1]
        after_y, after = bands[i + 1]
        if row_y - before_y > 10 or after_y - row_y > 10:
            continue
        names = [b for b in row if abs(center(b) - center(label)) <= 8 and re.fullmatch(r"[A-Za-z]+[0-9]+", b.text)]
        if len(names) != 1:
            continue
        for metric, unit in headings:
            means = [b for b in before if abs(center(b) - center(metric)) <= 4 and CELL.fullmatch(b.text) and "±" in b.text]
            records = [b for b in after if abs(center(b) - center(metric)) <= 4 and RECORD.fullmatch(b.text)]
            if len(means) == len(records) == 1:
                cells.extend((Cell(means[0], tuple(names), (metric, unit), "mean"),
                              Cell(records[0], tuple(names), (metric, unit), "champion")))
        for metric, unit in headings:
            singles = [b for b in row if abs(center(b) - center(metric)) <= 4 and CELL.fullmatch(b.text)]
            if len(singles) == 1:
                cells.append(Cell(singles[0], tuple(names), (metric, unit)))
    return cells


def _figure_claims(page, caption, support, blocks):
    # A caption must uniquely name the device for the explicitly named trace.
    caption_text = "\n".join(b.text for b in support)
    if not re.search(r"\bJ[–-]V\b", caption_text):
        return []
    traces = re.findall(r"\b([A-Z]\d+)(?:\s+structure)?\s*[, (]+(black|red|blue|green)\s+curve\b", caption_text)
    if len(traces) != len({color for _, color in traces}):
        return []
    fragments = []
    for name, color in traces:
        anchor = next(b for b in support if re.search(rf"\b{re.escape(name)}\b", b.text))
        row = Block(f"{anchor.block_id}-{name}", anchor.bbox, name)
        for i, block in enumerate(blocks):
            if not 0 <= block["bbox"][1] - caption.bbox[1] < 155:
                continue
            following = blocks[i+1] if i+1 < len(blocks) else None
            source_text = block["text"]
            rect = pymupdf.Rect(block["bbox"])
            if following and following["text"].startswith("and ") and (
                abs(following["bbox"][0] - rect.x0) < 5 and 0 <= following["bbox"][1] - rect.y1 < 8
            ):
                source_text += "\n" + following["text"]
                rect |= pymupdf.Rect(following["bbox"])
            claim = re.search(rf"(?:^|(?<=\. ))(The [\s\S]{{20,500}}?\bexhibited\s+"
                              rf"[\s\S]{{12,170}}?\({color} line\)\.)", source_text, re.I)
            if claim is None:
                continue
            bbox = tuple(rect)
            prefix = f"p{page.number + 1}-figure-{i}"
            witness = Block(prefix, bbox, claim[1])
            cells = []
            for j, match in enumerate(re.finditer(
                rf"\b(PCE|V[ _]*oc|J[ _]*sc|FF)\s*=\s*(({_NUMBER})(?:\s*(?:%|mV|V|mA/cm[²2]))?)",
                witness.text, re.I,
            )):
                heading = Block(f"{prefix}-label{j}", bbox, match[1])
                value = Block(f"{prefix}-value{j}", bbox, match[2])
                cells.append(Cell(value, (row,), (heading,), "champion"))
            if len(cells) < 3 or len({c.headers[0].text.casefold() for c in cells}) != len(cells):
                continue
            proof = (*support, witness)
            source = (*proof, row, *(b for cell in cells for b in (*cell.headers, cell.value)))
            fragments.append(Fragment(prefix, page.number + 1, source, tuple(cells),
                                      caption, _caption_location(caption), proof))
    return fragments


def _centered_row_groups(caption, lines):
    # A printed benchmark label can be vertically centred across two model rows.
    bands = _bands((b, y) for b, y in lines if abs(y - caption.bbox[1]) < 150)
    headings = [(i, y, row) for i, (y, row) in enumerate(bands)
                if len(row) >= 4 and [b.text.casefold() for b in row[:2]] == ["benchmark", "model"]]
    if len(headings) != 1:
        return []
    index, y, header = headings[0]
    if not 0 < caption.bbox[1] - y < 130 or any(
        y < b.bbox[1] < caption.bbox[1] and CAPTION.match(b.text)
        for b, _ in lines
    ):
        return []
    cells = []
    for i in range(index + 2, len(bands) - 1):
        middle_y, group = bands[i]
        top_y, top = bands[i - 1]
        bottom_y, bottom = bands[i + 1]
        if not 0 < middle_y - y < 120 or not 0 < middle_y - top_y < 9 or not 0 < bottom_y - middle_y < 9:
            continue
        if len(group) != 1 or abs(center(group[0]) - center(header[0])) > 11:
            continue
        if any(len(row) != len(header) - 1 or abs(center(row[0]) - center(header[1])) > 8
               or not all(_number(b) for b in row[1:]) for row in (top, bottom)):
            continue
        for row in (top, bottom):
            for value, heading in zip(row[1:], header[2:], strict=True):
                if heading.bbox[0] - 6 <= center(value) <= heading.bbox[2] + 6:
                    cells.append(Cell(value, (group[0], row[0]), (heading,)))
    return cells


def page_fragments(page, blocks, relevant, *, tables=True, previous_captions=None):
    numeric_superscripts = []
    lines = list(_lines(page, numeric_superscripts))
    captions = []
    for block, _ in lines:
        if CAPTION.match(block.text):
            captions.append(block)
    table_captions = [caption for caption in captions if _caption_location(caption).lower().startswith("table")]
    ruled = page.find_tables().tables if tables and table_captions else []
    fragments = []
    issues = []
    for caption in table_captions:
        location = _caption_location(caption)
        support = _caption_blocks(caption, lines)
        if re.search(r"\bCont\.?$", caption.text, re.I) and previous_captions:
            support += previous_captions.get(location.casefold(), ())
        if previous_captions is not None:
            previous_captions[location.casefold()] = support
        caption_text = " ".join(b.text for b in support)
        caption_has_reference = any(
            b.text.endswith(span["text"].strip()) and pymupdf.Rect(b.bbox).contains(pymupdf.Rect(span["bbox"]))
            for b in support if b.block_id.startswith(f"p{page.number + 1}-") for span in numeric_superscripts
        )
        # Geometric reconstruction often recovers merged headers in sparse ruled tables.
        cells = _borderless(caption, lines, support) or _ruled(page, caption, ruled)
        if not cells and re.search(r"average and record \(between parentheses\)", caption_text, re.I):
            cells = _mean_record(caption, lines)
        if not cells:
            cells = _centered_row_groups(caption, lines)
        if not cells:
            issues.append({"reason": "table_layout_unresolved", "page": page.number + 1,
                           "location": location, "quote": caption.text, "source_block_id": caption.block_id})
            continue
        for _, row in groupby(cells, key=lambda cell: cell.row):
            row = list(row)
            for start in range(0, len(row), 6):
                subset = tuple(row[start:start+6])
                source = list(support)
                for cell in subset:
                    source.extend((*cell.row, *cell.headers, cell.value))
                    if cell.row_axis:
                        source.append(cell.row_axis)
                fragments.append(Fragment(subset[0].value.block_id, page.number + 1,
                                          tuple(dict.fromkeys(source)), subset,
                                          caption, location, support, caption_has_reference=caption_has_reference))
    for caption in captions:
        if _caption_location(caption).lower().startswith("fig"):
            fragments.extend(_figure_claims(page, caption, _caption_blocks(caption, lines), blocks))
    for i, block in enumerate(blocks):
        text = block["text"]
        if len(text) < 35 or not relevant(text):
            continue
        # Preserve exact source text when splitting long blocks.
        for start in range(0, len(text), 1250):
            quote = text[start:start+1250]
            if relevant(quote):
                bid = f"p{page.number + 1}-text{i}-{start}"
                figure = CAPTION.match(quote)
                fragments.append(Fragment(bid, page.number + 1, (Block(bid, tuple(block["bbox"]), quote),),
                                          location=figure[1].replace(".", "") if figure else None))
    return fragments, issues
