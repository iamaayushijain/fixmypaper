"""
PDF Processor for detecting and annotating IEEE formatting compliance issues.
Checks IEEE-specific structural and formatting requirements in academic papers.

Key design principle:
    Every check emits one ErrorInstance PER OCCURRENCE of the problematic text,
    so every instance gets its own highlight annotation in the output PDF.

GROBID migration notes:
    - Text extraction:        GROBID TEI tokens replace PyMuPDF span heuristics.
    - Section headings:       GROBID <div><head> elements used as canonical source.
    - Abstract / keywords:    GROBID semantic tags checked first, regex as fallback.
    - Figure/table captions:  GROBID coords attribute replaces page-height guesses.
    - Figure count stat:      grobid_figures list replaces regex scan.
    - References existence:   raw_citations list replaces regex on full_text.
    - PDF open / image count / annotation writing: PyMuPDF retained (GROBID cannot do these).
    - Table extraction:       Camelot (lattice/stream).  GROBID table parsing removed.
"""
import os
import re
import json
import fitz  # PyMuPDF — retained for open/image-count/annotate only
import camelot
import requests
import urllib.parse
from collections import OrderedDict
from typing import List, Dict, Tuple, Optional, Set
from dataclasses import dataclass, field
from lxml import etree
# ---------------------------------------------------------------------------
# FORMAT CONFIGURATION CONSTANTS
# Used by the Streamlit professor/student UI to build and apply formats.
# ---------------------------------------------------------------------------

AVAILABLE_CHECKS: "OrderedDict[str, Dict]" = OrderedDict([
    # ── Metadata ─────────────────────────────────────────────────────────
    ("metadata_completeness", {
        "name": "Metadata Completeness",
        "description": "Title, authors, and publication date are present (GROBID header model)",
        "category": "Metadata",
        "error_types": ["metadata_incomplete"],
        "default": True,
    }),
    # ── Structure ────────────────────────────────────────────────────────
    # Section-existence is handled by a single format-driven check that runs
    # whenever a format specifies `mandatory_sections`. See the
    # `missing_required_section` error type and `_check_required_sections`.
    ("roman_numeral_headings", {
        "name": "Roman Numeral Section Headings",
        "description": "Section headings use Roman numerals (e.g. I. INTRODUCTION)",
        "category": "Structure",
        "error_types": ["non_roman_heading"],
        "default": True,
    }),
    # ── Numbering ────────────────────────────────────────────────────────
    ("figure_label_format", {
        "name": "Figure Label Format (Fig. N / Figure N)",
        "description": "Figures use 'Fig. N' or 'Figure N' convention",
        "category": "Numbering",
        "error_types": ["invalid_figure_label"],
        "default": True,
    }),
    ("table_label_format", {
        "name": "Table Label Format (TABLE I)",
        "description": "Tables use 'TABLE' all-caps with Roman numerals",
        "category": "Numbering",
        "error_types": ["invalid_table_numbering"],
        "default": True,
    }),
    ("equation_numbering", {
        "name": "Equation Numbering (1), (2), ...",
        "description": "Equations numbered sequentially in parentheses",
        "category": "Numbering",
        "error_types": ["equation_numbering"],
        "default": True,
    }),
    ("figure_sequential", {
        "name": "Sequential Figure Numbering",
        "description": "Figures numbered 1, 2, 3, ... with no gaps",
        "category": "Numbering",
        "error_types": ["figure_numbering_sequence"],
        "default": True,
    }),
    ("table_sequential", {
        "name": "Sequential Table Numbering",
        "description": "Tables numbered sequentially with no gaps",
        "category": "Numbering",
        "error_types": ["table_numbering_sequence"],
        "default": True,
    }),
    ("reference_sequential", {
        "name": "Sequential Reference Numbering [1],[2],[3]",
        "description": "References numbered [1],[2],[3],... with no gaps",
        "category": "Numbering",
        "error_types": ["reference_numbering_sequence"],
        "default": True,
    }),
    # ── Formatting ───────────────────────────────────────────────────────
    ("caption_placement", {
        "name": "Caption Placement (Fig below / Table above)",
        "description": "Figure captions below figures; table captions above tables",
        "category": "Formatting",
        "error_types": ["caption_placement"],
        "default": True,
    }),
    # ── References ───────────────────────────────────────────────────────
    ("reference_format", {
        "name": "Reference Format [n] Author, Title, ...",
        "description": "References formatted as [1] Author, Title, ...",
        "category": "References",
        "error_types": ["non_ieee_reference_format"],
        "default": True,
    }),
    ("url_doi_validity", {
        "name": "URL & DOI Validity",
        "description": "URLs and DOIs are well-formed and unbroken",
        "category": "References",
        "error_types": ["broken_url", "broken_doi"],
        "default": True,
    }),
    # ── Writing ──────────────────────────────────────────────────────────
    ("repeated_words", {
        "name": "Repeated Words",
        "description": "Consecutive repeated words (e.g. 'the the')",
        "category": "Writing",
        "error_types": ["repeated_word"],
        "default": False,
    }),
    ("et_al_formatting", {
        "name": "et al. Formatting",
        "description": "Correct usage: 'et al.' with period after 'al'",
        "category": "Writing",
        "error_types": ["citation_format"],
        "default": True,
    }),
    ("first_person_pronouns", {
        "name": "First-Person Pronouns (I, we, our)",
        "description": "Flags first-person pronouns in academic text",
        "category": "Writing",
        "error_types": ["writing_style"],
        "default": True,
    }),
    ("space_before_punctuation_marks", {
        "name": "Space Before Punctuation",
        "description": "Space immediately before , . ; : ! ? (e.g. 'hello , world')",
        "category": "Writing",
        "error_types": ["punctuation_spacing"],
        "default": True,
    }),
    ("double_punctuation", {
        "name": "Double Punctuation",
        "description": "Repeated or mixed terminal punctuation (e.g. 'end..' or 'what?!')",
        "category": "Writing",
        "error_types": ["punctuation_error"],
        "default": True,
    }),
    ("missing_space_after_punctuation_marks", {
        "name": "Missing Space After Punctuation",
        "description": "Punctuation not followed by a space, excluding decimals and abbreviations",
        "category": "Writing",
        "error_types": ["punctuation_spacing"],
        "default": True,
    }),
    ("comma_before_parenthesis", {
        "name": "Comma Before Parenthesis",
        "description": "Comma placed directly before an opening parenthesis (e.g. 'result ,(see')",
        "category": "Writing",
        "error_types": ["punctuation_error"],
        "default": True,
    }),
    ("punctuation_inside_citation", {
        "name": "Punctuation Inside Citation",
        "description": "Period placed after citation bracket mid-sentence (e.g. '[1]. shows')",
        "category": "Writing",
        "error_types": ["citation_format"],
        "default": True,
    }),
    ("multiple_spaces", {
        "name": "Multiple Spaces Between Words",
        "description": "Two or more consecutive spaces between words",
        "category": "Writing",
        "error_types": ["spacing_error"],
        "default": True,
    }),
    ("space_inside_brackets", {
        "name": "Space Inside Brackets",
        "description": "Space after opening or before closing parenthesis (e.g. '( value )')",
        "category": "Writing",
        "error_types": ["spacing_error"],
        "default": True,
    }),
])

ALL_SECTIONS: List[str] = [
    "Abstract",
    "Index Terms",
    "Introduction",
    "Related Work",
    "Background",
    "Methodology",
    "System Design",
    "Implementation",
    "Experiments",
    "Results",
    "Evaluation",
    "Discussion",
    "Conclusion",
    "Future Work",
    "Acknowledgments",
    "References",
]

SECTION_DETECTION_KEYWORDS: Dict[str, List[str]] = {
    "Abstract":       ["abstract"],
    "Index Terms":    ["index terms", "keywords", "key words"],
    "Introduction":   ["introduction"],
    "Related Work":   ["related work", "related", "prior work", "literature review", "literature"],
    "Background":     ["background", "preliminaries", "preliminary"],
    "Methodology":    ["methodology", "methods", "method", "proposed method", "approach"],
    "System Design":  ["system design", "design", "architecture", "system overview", "framework"],
    "Implementation": ["implementation", "experiment setup", "experimental setup"],
    "Experiments":    ["experiments", "experimental results", "experimental evaluation"],
    "Results":        ["results", "performance", "findings"],
    "Evaluation":     ["evaluation", "benchmark", "comparison"],
    "Discussion":     ["discussion", "analysis"],
    "Conclusion":     ["conclusion", "concluding remarks", "summary"],
    "Future Work":    ["future work", "future directions"],
    "Acknowledgments":["acknowledgment", "acknowledgement", "acknowledgments", "ACKNOWLEDGEMENT", "Gratitude"],
    "References":     ["references", "bibliography"],
}


@dataclass
class ErrorInstance:
    """Represents a single detected formatting issue in the PDF."""
    check_id: int
    check_name: str
    description: str
    page_num: int
    text: str
    bbox: Tuple[float, float, float, float]  # x0, y0, x1, y1
    error_type: str


class PDFErrorDetector:
    """Detects IEEE formatting compliance issues in research papers."""

    GROBID_URL = "http://localhost:8070"

    def __init__(self, start_page: int = 1):
        self.start_page_0 = max(0, start_page - 1)  # convert 1-indexed to 0-indexed
        self.full_text = ""
        self.page_texts: List[str] = []
        self.line_info: List[Tuple[str, Tuple, int]] = []   # (text, bbox, page_num)
        self.line_offsets: List[int] = []
        self.extracted_tables: List[Dict] = []
        self.total_tables_count = 0
        self.grobid_figures: List[Dict] = []
        self.grobid_tables: List[Dict] = []
        self.raw_citations: List[Dict] = []
        self.reference_analysis: Dict = {}
        self._grobid_equations: List[Dict] = []
        self.pix2text_equations: List[Dict] = []
        self.merged_blocks: List[Dict] = []
        self.merge_summary: Dict = {}
        self.pipeline_status: Dict = {
            "current_layer": {"success": False, "message": "Not started"},
            "pix2text": {"enabled": False, "success": False, "message": "Not started", "count": 0},
            "merge": {"success": False, "message": "Not started"},
        }

        # ── GROBID structural data ──────────────────────────────────────────
        # Populated by _extract_with_grobid(); used by multiple checks.
        self._grobid_section_heads: List[Dict] = []
        # Each entry: {"text": str, "page": int, "bbox": tuple-or-None}
        # Used by _check_roman_numeral_headings() and _detect_all_sections().

        self._grobid_has_abstract: bool = False
        self._grobid_abstract_text: str = ""
        self._grobid_has_keywords: bool = False
        # Used by the unified section-detection pipeline.

        # Populated by _detect_all_sections() — canonical section → presence info.
        # Shape: {section_name: {"found": bool, "matched_heading": str|None, "page": int|None}}
        self.section_detection: "OrderedDict[str, Dict]" = OrderedDict()

        self._grobid_figure_entries: List[Dict] = []
        # Full figure list with page + coords (kept for statistics only;
        # caption-placement no longer depends on GROBID — see _check_caption_placement).
        # (grobid_figures is the same list — kept foƒr backward compat with export_extracted_data.)

        self._grobid_table_entries: List[Dict] = []
        # Full table list with page + coords (kept for statistics only).

        self._grobid_metadata: Dict = {}
        # Populated by _extract_with_grobid(); holds parsed header fields:
        # {"title": str|None, "authors": [str], "date": str|None}
        # Used by _check_metadata_completeness().

    # =========================================================================
    # TEXT EXTRACTION  — now driven by GROBID TEI tokens
    # =========================================================================

    def _extract_all_text(self, doc: fitz.Document):
        """
        Primary path: ask GROBID processFulltextDocument for the TEI XML, then
        walk its <s> (sentence) and <w> (word/token) elements to rebuild
        line_info, page_texts, and full_text with proper page assignments.

        GROBID embeds page numbers in coords attributes like "5,72,334,480,12"
        (page, x0, y0, x1, y1 in PDF points).  We use those directly instead of
        re-running PyMuPDF span grouping, which struggles with multi-column layouts.

        If GROBID is unavailable the method falls back to the original PyMuPDF
        span-grouping logic so the rest of the pipeline keeps working.
        """
        try:
            self._extract_text_via_grobid(doc)
        except Exception as exc:
            print(f"[TEXT EXTRACT] GROBID text extraction failed ({exc}); "
                  "falling back to PyMuPDF span extraction.")
            self._extract_text_via_pymupdf(doc)

    # ------------------------------------------------------------------
    # GROBID-based text extraction
    # ------------------------------------------------------------------

    def _extract_text_via_grobid(self, doc: fitz.Document):
        """
        Rebuild line_info from GROBID <s> sentence elements.

        GROBID coords format: coords="page,x,y,w,h[;page,x,y,w,h…]"
        A sentence that wraps across N physical lines has N semicolon-separated
        fragments.  Storing the UNION bbox causes _calculate_match_bbox to
        interpolate within a tall multi-line rectangle, placing annotations on
        the wrong line.

        Fix: each fragment becomes its own line_info entry.  Text is
        proportionally split across fragments by their relative widths so
        character-interpolation in _calculate_match_bbox stays accurate within
        each single-line bbox.

        Path A — coords on <s>: split per fragment (main path after API fix).
        Path B — no coords: fall back to PyMuPDF page-text search.
        """
        root = self._tei_root  # AttributeError if absent → caller catches
        ns = {"tei": "http://www.tei-c.org/ns/1.0"}

        num_pages = len(doc)
        page_text_lists: List[List[str]] = [[] for _ in range(num_pages)]
        current_offset = 0

        # ── Collect sentence elements ────────────────────────────────────────
        sentences = root.findall(".//tei:s", ns)

        if not sentences:
            print("[GROBID] No <s> tags — synthesising from <p> elements")
            sentences = root.findall(".//tei:body//tei:p", ns) or root.findall(".//tei:p", ns)
            if not sentences:
                raise ValueError("No usable sentence or paragraph elements in GROBID TEI")

        # ── Detect whether coords are present ────────────────────────────────
        sample = sentences[:min(20, len(sentences))]
        has_coords = any(s.get("coords") for s in sample)

        if not has_coords:
            print("[GROBID] No coords on <s> elements — matching text to PyMuPDF pages")
            pymupdf_pages: List[str] = [doc[pg].get_text() for pg in range(num_pages)]

        last_page = max(0, self.start_page_0)

        # ── Main extraction loop ─────────────────────────────────────────────
        for sent in sentences:
            # Collect all visible text (child elements like <ref> contribute too)
            words: List[str] = []
            for token in sent.iter():
                t = (token.text or "").strip()
                if t:
                    words.append(t)
            if not words:
                continue

            line_text = " ".join(words)
            s_coords_raw = sent.get("coords", "")

            if s_coords_raw:
                # ── Path A: coords on <s> ────────────────────────────────────
                fragments = self._parse_grobid_fragments(s_coords_raw)
                if not fragments:
                    continue

                if len(fragments) == 1:
                    pg, x, y, w, h = fragments[0]
                    entries = [(line_text, pg, x, y, w, h)]
                else:
                    # Split text proportionally across line fragments
                    chunks = self._split_text_by_fragment_widths(line_text, fragments)
                    entries = [
                        (chunk, pg, x, y, w, h)
                        for chunk, (pg, x, y, w, h) in zip(chunks, fragments)
                        if chunk.strip()
                    ]

                for frag_text, pg, x, y, w, h in entries:
                    pg = min(pg, num_pages - 1)
                    last_page = pg
                    if pg < self.start_page_0:
                        continue
                    pw, ph = self._grobid_page_dims.get(pg, (595.276, 841.890))
                    bbox = (
                        max(0.0, x),
                        max(0.0, y),
                        min(pw, x + w),
                        min(ph, y + h),
                    )
                    self.line_info.append((frag_text, bbox, pg))
                    self.line_offsets.append(current_offset)
                    self.full_text += frag_text + "\n"
                    current_offset += len(frag_text) + 1
                    page_text_lists[pg].append(frag_text)

            else:
                # ── Path B: no coords — locate via PyMuPDF ───────────────────
                page_num, bbox = self._find_sentence_page(
                    line_text, pymupdf_pages, doc, search_from=last_page
                )
                page_num = min(page_num, num_pages - 1)
                last_page = page_num
                if page_num < self.start_page_0:
                    continue
                self.line_info.append((line_text, bbox, page_num))
                self.line_offsets.append(current_offset)
                self.full_text += line_text + "\n"
                current_offset += len(line_text) + 1
                page_text_lists[page_num].append(line_text)

        self.page_texts = ["\n".join(lines) for lines in page_text_lists]
        print(
            f"[TEXT EXTRACT] GROBID: {len(self.line_info)} line-fragments across "
            f"{num_pages} pages (coords={'yes' if has_coords else 'no'})."
        )

    # ── GROBID coord helpers ──────────────────────────────────────────────────

    def _parse_grobid_fragments(
        self, coords_raw: str
    ) -> List[Tuple[int, float, float, float, float]]:
        """
        Parse a GROBID coords string into a list of (page_0idx, x, y, w, h).

        Format: "page,x,y,w,h[;page,x,y,w,h…]"  (page is 1-indexed in GROBID)
        """
        result = []
        for frag in coords_raw.split(";"):
            parts = frag.strip().split(",")
            if len(parts) < 5:
                continue
            try:
                pg = int(parts[0]) - 1                  # convert to 0-indexed
                x, y, w, h = (float(p) for p in parts[1:5])
                result.append((pg, x, y, w, h))
            except ValueError:
                continue
        return result

    def _split_text_by_fragment_widths(
        self,
        text: str,
        fragments: List[Tuple[int, float, float, float, float]],
    ) -> List[str]:
        """
        Distribute *text* across *fragments* proportionally to each fragment's
        width (parts[3]).  Splits are made at the nearest word boundary so that
        each chunk is a coherent sub-phrase and _calculate_match_bbox can
        interpolate character positions correctly within a single-line bbox.
        """
        if len(fragments) <= 1:
            return [text]

        total_w = sum(f[3] for f in fragments)
        if total_w == 0:
            return [text] + [""] * (len(fragments) - 1)

        words = text.split()
        total_words = len(words)
        chunks: List[str] = []
        word_idx = 0

        for i, (_, _, _, w, _) in enumerate(fragments):
            if i == len(fragments) - 1:
                # Last fragment gets whatever remains
                chunks.append(" ".join(words[word_idx:]))
                break

            # Target word count for this fragment
            target = max(1, round(total_words * w / total_w))
            end = min(word_idx + target, total_words)
            # Don't overshoot — leave at least 1 word for remaining fragments
            end = min(end, total_words - (len(fragments) - i - 1))
            end = max(end, word_idx + 1)

            chunks.append(" ".join(words[word_idx:end]))
            word_idx = end

        # Pad if fragments outnumber available chunks (very short sentences)
        while len(chunks) < len(fragments):
            chunks.append("")

        return chunks

    def _find_sentence_page(
        self,
        line_text: str,
        pymupdf_pages: List[str],
        doc: fitz.Document,
        search_from: int = 0,
    ) -> Tuple[int, Tuple[float, float, float, float]]:
        """
        Locate *line_text* in the PyMuPDF page texts and return (page_num, bbox).

        We build a 4–6 word search key from the middle of the sentence to
        avoid edge-word hyphenation artefacts.  The search proceeds forward
        from *search_from* (preserving document order) and wraps around if
        needed.
        """
        words = line_text.split()
        if not words:
            return search_from, (0.0, 0.0, 100.0, 14.0)

        # Build a distinctive substring (middle 4-6 words, min 15 chars)
        mid = max(0, len(words) // 2 - 2)
        key_words = words[mid: mid + 6]
        search_key = " ".join(key_words)
        if len(search_key) < 10 and words:
            search_key = " ".join(words[:min(6, len(words))])

        # Normalise whitespace for matching
        search_key_norm = re.sub(r"\s+", " ", search_key).strip()

        num_pages = len(pymupdf_pages)
        page_order = list(range(search_from, num_pages)) + list(range(0, search_from))

        for pg in page_order:
            page_text = pymupdf_pages[pg]
            page_norm = re.sub(r"\s+", " ", page_text)
            if search_key_norm.lower() in page_norm.lower():
                # Try to get an actual bbox via PyMuPDF text search
                try:
                    rects = doc[pg].search_for(search_key_norm)
                    if rects:
                        r = rects[0]
                        return pg, (r.x0, r.y0, r.x1, r.y1)
                except Exception:
                    pass
                return pg, (0.0, 0.0, 595.0, 14.0)

        # Not found anywhere — keep last known page
        return search_from, (0.0, 0.0, 595.0, 14.0)

    def _parse_grobid_coords(
        self,
        coords_list: List[str],
        fallback_page: int = 0,
    ) -> Tuple[int, Tuple[float, float, float, float]]:
        """
        Parse a list of GROBID coords strings and return
        (page_num, union_bbox) covering all tokens.

        GROBID coord format: "page,x,y,w,h"  (may be multi-fragment:
        "page,x,y,w,h;page,x,y,w,h;…")

          • page  — 1-indexed; we convert to 0-indexed
          • x, y  — top-left corner in PDF points, origin = top-left of page
            (same as PyMuPDF — no Y-axis flip needed)
          • w, h  — width and height in PDF points

        The union bbox (min x0, min y0, max x1, max y1) across all fragments
        is returned so the entire token span is highlighted.
        """
        if not coords_list:
            return fallback_page, (0.0, 0.0, 100.0, 14.0)

        pages = []
        x0s, y0s, x1s, y1s = [], [], [], []

        for raw in coords_list:
            fragments = raw.split(";") if ";" in raw else [raw]
            for frag in fragments:
                parts = frag.strip().split(",")
                if len(parts) < 5:
                    continue
                try:
                    pg   = int(parts[0]) - 1        # 0-indexed
                    x    = float(parts[1])
                    y    = float(parts[2])
                    w    = float(parts[3])           # width  (NOT x1)
                    h    = float(parts[4])           # height (NOT y1)
                    pages.append(pg)
                    x0s.append(x)
                    y0s.append(y)
                    x1s.append(x + w)               # derive x1
                    y1s.append(y + h)               # derive y1
                except ValueError:
                    continue

        if not x0s:
            return fallback_page, (0.0, 0.0, 100.0, 14.0)

        page_num = pages[0]
        # Clamp to page bounds if we have the page dimensions
        dims = getattr(self, "_grobid_page_dims", {})
        pw, ph = dims.get(page_num, (595.276, 841.890))
        bbox = (
            max(0.0, min(x0s)),
            max(0.0, min(y0s)),
            min(pw,  max(x1s)),
            min(ph,  max(y1s)),
        )
        return page_num, bbox

    # ------------------------------------------------------------------
    # PyMuPDF fallback text extraction (original implementation)
    # ------------------------------------------------------------------

    def _extract_text_via_pymupdf(self, doc: fitz.Document):
        """Original PyMuPDF span-grouping extraction — used only as fallback."""
        current_offset = 0

        for page_num in range(len(doc)):
            page = doc[page_num]
            self.page_texts.append(page.get_text())

            if page_num < self.start_page_0:
                continue

            page_spans = []
            for block in page.get_text("dict").get("blocks", []):
                if block.get("type") == 0:
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            if span.get("text", "").strip():
                                page_spans.append({
                                    "text": span["text"],
                                    "bbox": span["bbox"],
                                    "page_num": page_num,
                                })

            for line_spans in self._group_spans_by_line(page_spans):
                line_text = "".join(s["text"] for s in line_spans).strip()
                if not line_text:
                    continue

                x0 = min(s["bbox"][0] for s in line_spans)
                y0 = min(s["bbox"][1] for s in line_spans)
                x1 = max(s["bbox"][2] for s in line_spans)
                y1 = max(s["bbox"][3] for s in line_spans)

                self.line_info.append((line_text, (x0, y0, x1, y1), page_num))
                self.line_offsets.append(current_offset)
                self.full_text += line_text + "\n"
                current_offset += len(line_text) + 1

    def _group_spans_by_line(self, spans: list, tolerance: int = 3) -> list:
        """Group spans sharing the same vertical position into lines."""
        if not spans:
            return []

        spans_sorted = sorted(
            spans,
            key=lambda s: (round(s["bbox"][1] / tolerance), s["bbox"][0])
        )

        lines, current_line = [], [spans_sorted[0]]
        current_y = round(spans_sorted[0]["bbox"][1] / tolerance) * tolerance

        for span in spans_sorted[1:]:
            span_y = round(span["bbox"][1] / tolerance) * tolerance
            if abs(span_y - current_y) <= tolerance:
                current_line.append(span)
            else:
                lines.append(current_line)
                current_line = [span]
                current_y = span_y

        if current_line:
            lines.append(current_line)

        return lines

    # =========================================================================
    # TABLE EXTRACTION  (Camelot)
    # =========================================================================

    def _extract_tables(self, pdf_path: str):
        """Extract tables using Camelot and analyze them."""
        try:
            print("[TABLE EXTRACTION] Extracting tables from PDF...")
            tables = camelot.read_pdf(pdf_path, pages="all", flavor="lattice")

            if tables.n == 0:
                print("[TABLE EXTRACTION] Lattice found 0 tables, trying stream method...")
                tables = camelot.read_pdf(pdf_path, pages="all", flavor="stream")

            print(f"[TABLE EXTRACTION] Initially found {tables.n} tables")

            raw_tables = []
            for idx, table in enumerate(tables):
                if table.page <= self.start_page_0:
                    continue
                # Camelot reports bbox as (x1, y1_bottom, x2, y2_top) in
                # PDF-native bottom-left coords.  We also stash the page
                # height so callers (caption-placement check) can convert
                # to PyMuPDF's top-left origin.
                cam_bbox = getattr(table, "_bbox", None)
                try:
                    page_h = float(table.page_height) if hasattr(table, "page_height") else None
                except Exception:
                    page_h = None
                table_data = {
                    "index": idx,
                    "page": table.page,
                    "dataframe": table.df,
                    "headers": table.df.iloc[0].tolist() if len(table.df) > 0 else [],
                    "accuracy": table.accuracy,
                    "whitespace": table.whitespace,
                    "bbox_pdf": tuple(cam_bbox) if cam_bbox else None,  # bottom-left origin
                    "page_height": page_h,
                }
                raw_tables.append(table_data)
                print(f"[TABLE EXTRACTION] Table {idx+1} on page {table.page}: "
                      f"{len(table.df.columns)} columns, "
                      f"headers: {table_data['headers'][:3]}...")

            self.extracted_tables = self._merge_adjacent_tables(raw_tables)
            self.total_tables_count = len(self.extracted_tables)
            print(f"[TABLE EXTRACTION] After merging: {self.total_tables_count} tables")

        except Exception as e:
            print(f"[TABLE EXTRACTION] Error: {e}")
            import traceback; traceback.print_exc()
            self.total_tables_count = 0
            self.extracted_tables = []

    def _merge_adjacent_tables(self, tables: list) -> list:
        if len(tables) <= 1:
            return tables

        merged_tables, i = [], 0
        while i < len(tables):
            current_table = tables[i]
            current_headers = current_table["headers"]
            j = i + 1
            tables_to_merge = [current_table]

            while j < len(tables):
                next_table = tables[j]
                if self._headers_match(current_headers, next_table["headers"]):
                    print(f"[TABLE MERGE] Merging table {current_table['index']} "
                          f"(page {current_table['page']}) with table "
                          f"{next_table['index']} (page {next_table['page']})")
                    tables_to_merge.append(next_table)
                    j += 1
                else:
                    break

            if len(tables_to_merge) > 1:
                merged = self._merge_table_data(tables_to_merge)
                merged_tables.append(merged)
                print(f"[TABLE MERGE] Created merged table with "
                      f"{len(merged['dataframe'])} total rows")
            else:
                merged_tables.append(current_table)
            i = j

        for idx, table in enumerate(merged_tables):
            table["index"] = idx
        return merged_tables

    def _headers_match(self, headers1: list, headers2: list) -> bool:
        if len(headers1) != len(headers2):
            return False
        for h1, h2 in zip(headers1, headers2):
            if str(h1).strip().lower() != str(h2).strip().lower():
                return False
        return True

    def _merge_table_data(self, tables: list) -> dict:
        import pandas as pd
        merged_df = tables[0]["dataframe"].copy()
        for table in tables[1:]:
            extra = table["dataframe"].iloc[1:] if len(table["dataframe"]) > 1 else table["dataframe"]
            merged_df = pd.concat([merged_df, extra], ignore_index=True)

        return {
            "index": tables[0]["index"],
            "page": tables[0]["page"],
            "pages": [t["page"] for t in tables],
            "dataframe": merged_df,
            "headers": tables[0]["headers"],
            "accuracy": sum(t["accuracy"] for t in tables) / len(tables),
            "whitespace": sum(t["whitespace"] for t in tables) / len(tables),
            "merged_from": len(tables),
        }

    # =========================================================================
    # GROBID INTEGRATION  — extended to populate structural metadata
    # =========================================================================

    def _extract_with_grobid(self, pdf_path: str):
        """
        Use GROBID processFulltextDocument to extract:
          • figures and their captions  → grobid_figures / _grobid_figure_entries
          • tables and their captions   → grobid_tables  / _grobid_table_entries
          • section headings            → _grobid_section_heads
          • abstract presence           → _grobid_has_abstract
          • keywords/index-terms presence → _grobid_has_keywords
          • TEI root for text extraction → _tei_root

        All of these feed the compliance checks, replacing heuristic approaches.
        """
        try:
            print("[GROBID] Processing PDF with GROBID...")
            with open(pdf_path, "rb") as pdf_file:
                response = requests.post(
                    f"{self.GROBID_URL}/api/processFulltextDocument",
                    files={"input": pdf_file},
                    timeout=180,
                    # teiCoordinates must be a repeated form field — one entry per
                    # element type.  A dict collapses duplicates, so use a list of
                    # tuples.  "s" gives sentence-level bounding boxes which are the
                    # primary input for text-extraction and PDF annotation.
                    data=[
                        ("teiCoordinates", "s"),
                        ("teiCoordinates", "ref"),
                        ("teiCoordinates", "figure"),
                        ("teiCoordinates", "formula"),
                        ("segmentSentences", "1"),
                        ("consolidateHeader", "1"),
                        ("consolidateCitations", "1"),
                    ],
                )

            if response.status_code != 200:
                print(f"[GROBID] Error: status code {response.status_code}")
                self._tei_root = None
                return

            tei_xml = response.content

            base_name = os.path.splitext(os.path.basename(pdf_path))[0]

            # Create output directory (optional but recommended)
            output_dir = os.path.join(os.path.dirname(pdf_path), "grobid_outputs")
            os.makedirs(output_dir, exist_ok=True)

            # Final path
            tei_output_path = os.path.join(output_dir, f"{base_name}.tei.xml")

            # Save file
            with open(tei_output_path, "wb") as f:
                f.write(tei_xml)

            print(f"[GROBID] TEI saved at: {tei_output_path}")

            
            root = etree.fromstring(tei_xml)
            self._tei_root = root  # stored for _extract_text_via_grobid()

            ns = {"tei": "http://www.tei-c.org/ns/1.0"}

            # ── Page dimensions from <facsimile> ─────────────────────────────
            # GROBID coords are (page, x, y, w, h) in a top-left origin system
            # whose unit is PDF points.  The <facsimile><surface> elements give
            # us the precise page height for each page so we can convert to
            # PyMuPDF rects correctly.  Keyed 0-indexed to match self.line_info.
            self._grobid_page_dims: Dict[int, Tuple[float, float]] = {}
            for surface in root.findall(".//tei:facsimile/tei:surface", ns):
                try:
                    n = int(surface.get("n", 0)) - 1       # 0-indexed
                    w = float(surface.get("lrx", 595.276))
                    h = float(surface.get("lry", 841.890))
                    self._grobid_page_dims[n] = (w, h)
                except (ValueError, TypeError):
                    continue
            print(f"[GROBID] Loaded page dims for {len(self._grobid_page_dims)} pages")

            # ── Header metadata (title / authors / date) ────────────────────
            # GROBID's header model populates these via processFulltextDocument.
            # <title level="a" type="main"> holds the paper title.
            # <analytic><author> elements hold each author.
            # <publicationStmt><date> or <imprint><date> holds publication year.
            title_el = root.find(
                ".//tei:fileDesc/tei:titleStmt/tei:title[@type='main']", ns
            )
            if title_el is None:
                title_el = root.find(".//tei:fileDesc/tei:titleStmt/tei:title", ns)
            title_text = "".join(title_el.itertext()).strip() if title_el is not None else ""

            authors = []
            for author_el in root.findall(".//tei:analytic/tei:author", ns):
                surname = author_el.findtext(".//tei:surname", default="", namespaces=ns)
                forename = author_el.findtext(".//tei:forename", default="", namespaces=ns)
                name = f"{forename} {surname}".strip()
                if name:
                    authors.append(name)

            # Try publicationStmt first, then imprint inside sourceDesc
            date_el = root.find(".//tei:fileDesc/tei:publicationStmt/tei:date", ns)
            if date_el is None:
                date_el = root.find(".//tei:sourceDesc//tei:imprint/tei:date", ns)
            date_text = ""
            if date_el is not None:
                date_text = date_el.get("when", "") or "".join(date_el.itertext()).strip()

            self._grobid_metadata = {
                "title":   title_text or None,
                "authors": authors,
                "date":    date_text or None,
            }
            print(f"[GROBID] Metadata — title: {bool(title_text)}, "
                  f"authors: {len(authors)}, date: {bool(date_text)}")

            # ── Abstract ────────────────────────────────────────────────────
            # GROBID wraps the abstract in <abstract> inside <profileDesc>.
            abstract_el = root.find(".//tei:profileDesc/tei:abstract", ns)
            if abstract_el is not None:
                abstract_text = "".join(abstract_el.itertext()).strip()
                self._grobid_has_abstract = bool(abstract_text)
                self._grobid_abstract_text = abstract_text
            else:
                self._grobid_has_abstract = False
                self._grobid_abstract_text = ""
            word_count = len(self._grobid_abstract_text.split())
            print(f"[GROBID] Abstract found: {self._grobid_has_abstract} ({word_count} words)")

            # ── Keywords / Index Terms ───────────────────────────────────────
            # GROBID puts keywords in <textClass><keywords>.
            keywords_el = root.find(".//tei:profileDesc/tei:textClass/tei:keywords", ns)
            if keywords_el is not None:
                kw_text = "".join(keywords_el.itertext()).strip()
                self._grobid_has_keywords = bool(kw_text)
            else:
                self._grobid_has_keywords = False
            print(f"[GROBID] Keywords found: {self._grobid_has_keywords}")

            # ── Section headings ────────────────────────────────────────────
            # Every <div><head> in the body is a section or subsection heading.
            self._grobid_section_heads = []
            for head_el in root.findall(".//tei:body//tei:div/tei:head", ns):
                head_text = (head_el.text or "").strip()
                if not head_text:
                    head_text = "".join(head_el.itertext()).strip()
                if not head_text:
                    continue

                page_num, bbox = self._parse_grobid_coords(
                    [head_el.get("coords", "")], fallback_page=0
                )
                self._grobid_section_heads.append({
                    "text": head_text,
                    "page": page_num,
                    "bbox": bbox,
                })

            print(f"[GROBID] Section headings found: {len(self._grobid_section_heads)}")

            # ── Figures ─────────────────────────────────────────────────────
            # GROBID wraps many float objects (figures, algorithms, charts)
            # in <figure>.  To get an accurate count we only keep entries
            # that carry a recognisable "Fig." / "Figure" label with a
            # number, and we deduplicate by that number so multi-panel
            # entries are not double-counted.
            self.grobid_figures = []
            self._grobid_figure_entries = []
            _fig_label_re = re.compile(r'(?:Fig\.?|Figure)\s*(\d+)', re.IGNORECASE)
            _seen_fig_nums = set()
            fig_idx = 0

            for fig in root.findall(".//tei:figure", ns):
                if fig.get("type") == "table":
                    continue

                # Extract label, head, xml:id and figDesc
                label_el = fig.find(".//tei:label", ns)
                head = fig.find(".//tei:head", ns)
                fig_desc = fig.find(".//tei:figDesc", ns)
                xml_id = fig.get("{http://www.w3.org/XML/1998/namespace}id", "") or fig.get("xml:id", "")

                label = ""
                if label_el is not None:
                    label = ("".join(label_el.itertext())).strip()
                if not label and head is not None:
                    label = ("".join(head.itertext())).strip()

                description = ""
                if fig_desc is not None:
                    description = ("".join(fig_desc.itertext())).strip()

                caption = f"{label} {description}".strip() if label else description

                # Determine the figure number from label text first,
                # then fall back to GROBID's xml:id (e.g. "fig_0" → 1).
                fig_num = None
                lm = _fig_label_re.search(label) if label else None
                if lm:
                    fig_num = int(lm.group(1))
                elif not lm and caption:
                    lm = _fig_label_re.search(caption)
                    if lm:
                        fig_num = int(lm.group(1))
                if fig_num is None and xml_id:
                    xm = re.search(r'fig_(\d+)', xml_id)
                    if xm:
                        fig_num = int(xm.group(1)) + 1  # GROBID uses 0-based ids

                # Skip entries that have no recognisable figure number —
                # they are usually algorithms, pseudo-code, or decoration.
                if fig_num is None:
                    continue

                # Deduplicate by figure number (multi-panel / repeated refs)
                if fig_num in _seen_fig_nums:
                    continue
                _seen_fig_nums.add(fig_num)

                coords_str = fig.get("coords", "")
                page_num, bbox = self._parse_grobid_coords(
                    [coords_str] if coords_str else [], fallback_page=0
                )

                entry = {
                    "index": fig_idx,
                    "type": "figure",
                    "label": label,
                    "number": fig_num,
                    "description": description,
                    "caption": caption,
                    "xml_coords": coords_str,
                    "page": page_num,
                    "bbox": bbox,
                }
                self.grobid_figures.append(entry)
                self._grobid_figure_entries.append(entry)
                fig_idx += 1
                print(f"[GROBID] Figure {fig_num} (page {page_num+1}): {caption[:60]}...")

            # ── Tables: extracted by Camelot (not GROBID) ──────────────────
            # grobid_tables / _grobid_table_entries are left empty so that
            # Caption placement is handled by _check_caption_placement (PyMuPDF).
            self.grobid_tables = []
            self._grobid_table_entries = []

            # ── Equations ──────────────────────────────────────────────────
            # GROBID emits <formula type="display"> for numbered / block
            # equations and <formula type="inline"> for inline math.
            # Only display equations should be counted and checked.
            # The equation number is often in a <label> child element
            # (e.g. <label>(1)</label>) rather than in the running text.
            self._grobid_equations = []
            _seen_eq_nums = set()
            display_idx = 0

            for formula in root.findall(".//tei:formula", ns):
                ftype = (formula.get("type") or "").lower()
                # Keep display (numbered) equations; skip inline math
                if ftype == "inline":
                    continue

                text = "".join(formula.itertext()).strip()
                if not text:
                    continue

                # Collect all coord fragments (GROBID may emit semicolon-
                # separated multi-token coords like "1,x0,y0,x1,y1;1,…").
                raw_coords = formula.get("coords", "")
                coord_parts = [c.strip() for c in raw_coords.split(";") if c.strip()] \
                              if ";" in raw_coords else ([raw_coords] if raw_coords else [])
                page_num, bbox = self._parse_grobid_coords(
                    coord_parts, fallback_page=0
                )

                # Try to read equation number from <label> first
                eq_num = None
                label_el = formula.find(".//tei:label", ns)
                if label_el is not None:
                    label_txt = ("".join(label_el.itertext())).strip()
                    lm = re.search(r'\(?(\d+)\)?', label_txt)
                    if lm:
                        eq_num = int(lm.group(1))

                # Fall back to regex on the full text
                if eq_num is None:
                    num_match = re.search(r'\((\d+)\)\s*$', text)
                    if num_match:
                        eq_num = int(num_match.group(1))

                # Deduplicate by equation number
                if eq_num is not None and eq_num in _seen_eq_nums:
                    continue
                if eq_num is not None:
                    _seen_eq_nums.add(eq_num)

                entry = {
                    "index": display_idx,
                    "text": text,
                    "number": eq_num,
                    "page": page_num,
                    "bbox": bbox,
                }
                self._grobid_equations.append(entry)
                display_idx += 1

            print(f"[GROBID] Extracted {len(self.grobid_figures)} figures "
                  f"and {len(self._grobid_equations)} display equations "
                  f"(tables extracted separately by Camelot)")

        except requests.exceptions.Timeout:
            print("[GROBID] Timeout — GROBID service took too long")
            self._tei_root = None
        except requests.exceptions.ConnectionError:
            print("[GROBID] Connection error — GROBID service not available")
            self._tei_root = None
        except Exception as e:
            print(f"[GROBID] Error: {e}")
            import traceback; traceback.print_exc()
            self._tei_root = None

    # ── Reference entry patterns ─────────────────────────────────────────────
    _REF_HEADING_RE = re.compile(
        r"^\s*(?:[IVXivxlcdm]+[\.\)]\s*)?(?:REFERENCES?|BIBLIOGRAPHY|WORKS?\s+CITED)\s*$",
        re.IGNORECASE,
    )
    _REF_ENTRY_PATTERNS = [
        re.compile(r"^\s*\[(\d+)\]\s+\S"),     # IEEE: [1] text
        re.compile(r"^\s*(\d+)\.\s+\S"),        # APA/MLA: 1. text
        re.compile(r"^\s*\((\d+)\)\s+\S"),      # Vancouver: (1) text
        re.compile(r"^\s*(\d+)\)\s+\S"),        # 1) text
    ]

    def _extract_citations_from_pdf(self, doc: "fitz.Document") -> List[Dict]:
        """
        Extract references directly from PDF text using PyMuPDF.

        Finds the References/Bibliography section heading, then groups
        multi-line reference entries using the numbering pattern detected
        from the first few entries.  Returns raw text exactly as it appears
        in the paper — no field reconstruction.

        Falls back to _extract_citations_grobid_fallback() if no reference
        section or no entries are found.
        """
        # Gather all lines with their page numbers
        all_lines: List[Tuple[int, str]] = []
        for pg in range(len(doc)):
            for line in doc[pg].get_text().splitlines():
                all_lines.append((pg, line))

        # Locate the references section heading
        ref_start_idx: Optional[int] = None
        for idx, (_, line) in enumerate(all_lines):
            if self._REF_HEADING_RE.match(line.strip()):
                ref_start_idx = idx
                break

        if ref_start_idx is None:
            print("[CITATIONS] No references heading found; trying GROBID fallback")
            return self._extract_citations_grobid_fallback(
                doc.name if hasattr(doc, "name") else ""
            )

        ref_lines = all_lines[ref_start_idx + 1:]

        # Detect which numbering style is used from the first non-empty lines
        entry_re: Optional[re.Pattern] = None
        for _, line in ref_lines[:30]:
            stripped = line.strip()
            if not stripped:
                continue
            for pat in self._REF_ENTRY_PATTERNS:
                if pat.match(stripped):
                    entry_re = pat
                    break
            if entry_re:
                break

        if entry_re is None:
            print("[CITATIONS] Could not detect reference numbering style; trying GROBID fallback")
            return self._extract_citations_grobid_fallback(
                doc.name if hasattr(doc, "name") else ""
            )

        # Group continuation lines into single reference entries
        citations: List[Dict] = []
        current_parts: List[str] = []
        current_page: Optional[int] = None

        for page_num, line in ref_lines:
            stripped = line.strip()
            if not stripped:
                continue
            if entry_re.match(stripped):
                if current_parts:
                    raw = " ".join(current_parts)
                    if len(raw) > 10:
                        citations.append({"raw_text": raw, "page": current_page})
                current_parts = [stripped]
                current_page = page_num
            elif current_parts:
                current_parts.append(stripped)

        if current_parts:
            raw = " ".join(current_parts)
            if len(raw) > 10:
                citations.append({"raw_text": raw, "page": current_page})

        print(f"[CITATIONS] Extracted {len(citations)} references from PDF text")

        if not citations:
            print("[CITATIONS] No entries found; trying GROBID fallback")
            return self._extract_citations_grobid_fallback(
                doc.name if hasattr(doc, "name") else ""
            )

        return citations

    def _extract_citations_grobid_fallback(self, pdf_path: str) -> List[Dict]:
        """
        Fallback: call GROBID /api/processReferences and return raw_text
        built from itertext() on each <biblStruct> — preserves more original
        text than the previous field-reconstruction approach.
        """
        citations: List[Dict] = []
        if not pdf_path or not os.path.exists(pdf_path):
            return citations
        try:
            print("[GROBID CITATIONS] Fallback: extracting references via GROBID...")
            with open(pdf_path, "rb") as f:
                response = requests.post(
                    f"{self.GROBID_URL}/api/processReferences",
                    files={"input": f},
                    timeout=60,
                )
            if response.status_code != 200:
                print(f"[GROBID CITATIONS] Error: status {response.status_code}")
                return citations

            root = etree.fromstring(response.content)
            ns = {"tei": "http://www.tei-c.org/ns/1.0"}

            for ref in root.findall(".//tei:biblStruct", ns):
                # Use itertext() to get all text content without field reconstruction
                raw_text = " ".join(" ".join(ref.itertext()).split())
                if raw_text and len(raw_text) > 10:
                    citations.append({"raw_text": raw_text})

            print(f"[GROBID CITATIONS] Fallback extracted {len(citations)} references")

        except requests.exceptions.Timeout:
            print("[GROBID CITATIONS] Timeout — GROBID service took too long")
        except requests.exceptions.ConnectionError:
            print("[GROBID CITATIONS] Connection error — GROBID not available")
        except Exception as e:
            print(f"[GROBID CITATIONS] Error: {e}")
            import traceback; traceback.print_exc()

        return citations

    @staticmethod
    def _analyze_references_via_openai(citations: List[Dict]) -> Dict:
        """
        Fallback reference analysis using OpenAI Responses API.
        Returns the same high-level shape as reference-api for UI compatibility.
        """
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            return {"error": "OpenAI fallback unavailable: OPENAI_API_KEY not set"}

        model = os.environ.get("OPENAI_REF_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
        base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        endpoint = f"{base_url}/responses"
        timeout_s = int(os.environ.get("OPENAI_REF_TIMEOUT_SECONDS", "60") or "60")

        refs = [str(c.get("raw_text", "")).strip() for c in citations if str(c.get("raw_text", "")).strip()]
        if not refs:
            return {"error": "No reference text available for OpenAI fallback"}

        input_json = json.dumps(refs, ensure_ascii=False)
        prompt = (
            "You are a research reference parser and quality checker. "
            "Your input is an array of reference strings extracted from a PDF by GROBID, an imperfect parser. "
            "The strings are often garbled: author names may be embedded mid-string after the title, "
            "punctuation and delimiters may be missing, and fields may be out of order.\n\n"

            "PARSING RULES — use common sense:\n"
            "- Any 4-digit number between 1900 and 2026 is a YEAR. Always. Even if it appears at the end without parentheses.\n"
            "- Clusters of 'Lastname Initials' tokens (e.g. 'Arnoldi W E', 'Athenstaedt H') are AUTHORS, "
            "even if they appear after the title or mid-string.\n"
            "- The longest descriptive noun phrase is the TITLE.\n"
            "- A short recognizable word cluster (e.g. 'Science', 'Quart. Appl. Math', 'J. Biomech') is the JOURNAL.\n"
            "- Small numbers at the end (e.g. '9', '216', '13-15') are VOLUME or PAGES — not years, not IDs.\n"
            "- If a string contains 'doi:' or 'https://doi.org/', extract it as the DOI.\n\n"

            "LENIENCY RULES — only flag genuinely missing fields:\n"
            "- Do NOT flag 'authors missing' if any human-name-like token exists anywhere in the string.\n"
            "- Do NOT flag 'year missing' if any 4-digit number between 1900 and 2026 exists anywhere.\n"
            "- Do NOT flag 'DOI missing' as an error — it is optional. Only mention it as a soft suggestion.\n"
            "- Only raise a COMPLETENESS issue if a field is completely and unambiguously absent.\n\n"

            "OUTPUT: Produce ONLY valid JSON matching this exact schema — no markdown, no explanation:\n"
            "{\n"
            "  \"summary\": {\n"
            "    \"total\": number,\n"
            "    \"parsed_ok\": number,\n"
            "    \"total_issues\": number,\n"
            "    \"style\": string,\n"
            "    \"checks_passed\": string[],\n"
            "    \"checks_failed\": string[]\n"
            "  },\n"
            "  \"list_level_issues\": [\n"
            "    {\"position\": string, \"check\": string, \"detail\": string, \"expected\": string|null}\n"
            "  ],\n"
            "  \"entries\": [\n"
            "    {\n"
            "      \"id\": string,\n"
            "      \"raw_text\": string,\n"
            "      \"parsed\": {\n"
            "        \"title\": string|null,\n"
            "        \"authors\": string[],\n"
            "        \"pub_date\": string|null,\n"
            "        \"doi\": string|null\n"
            "      },\n"
            "      \"issues\": [\n"
            "        {\"check\": string, \"field\": string|null, \"detail\": string, \"suggestion\": string|null}\n"
            "      ]\n"
            "    }\n"
            "  ]\n"
            "}\n\n"

            "Keep ids as [1], [2], ... matching input order. "
            "If you are unsure about a field, make your best guess rather than leaving it null. "
            "Guessing is better than false 'missing field' errors.\n\n"

            f"Reference entries JSON array:\n{input_json}"
        )
        payload = {
            "model": model,
            "input": prompt,
            "text": {"format": {"type": "json_object"}},
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            print(f"[OPENAI REF FALLBACK] Sending {len(refs)} references using model={model}")
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=timeout_s)
            resp.raise_for_status()
            body = resp.json()

            output_text = body.get("output_text")
            if not output_text:
                chunks = []
                for item in body.get("output", []):
                    for content in item.get("content", []):
                        if content.get("type") == "output_text":
                            chunks.append(content.get("text", ""))
                output_text = "\n".join(chunks).strip()

            if not output_text:
                return {"error": "OpenAI fallback returned empty output"}

            parsed = json.loads(output_text)
            if not isinstance(parsed, dict):
                return {"error": "OpenAI fallback returned non-object JSON"}

            parsed.setdefault("summary", {})
            parsed.setdefault("entries", [])
            parsed.setdefault("list_level_issues", [])
            print(f"[OPENAI REF FALLBACK] Success: {parsed.get('summary', {})}")
            return parsed
        except requests.exceptions.Timeout:
            print("[OPENAI REF FALLBACK] Timeout")
            return {"error": "OpenAI fallback timed out"}
        except requests.exceptions.ConnectionError as e:
            print(f"[OPENAI REF FALLBACK] Connection error: {e}")
            return {"error": "OpenAI fallback connection error"}
        except Exception as e:
            print(f"[OPENAI REF FALLBACK] Error: {e}")
            return {"error": f"OpenAI fallback failed: {e}"}

    @staticmethod
    def analyze_references(citations: List[Dict]) -> Dict:
        """Analyze references via OpenAI."""
        if not citations:
            return {}

        print(f"[OpenAI] Analyzing {len(citations)} references...")
        return PDFErrorDetector._analyze_references_via_openai(citations)

    @staticmethod
    def _bbox_overlap_ratio(a: Dict, b: Dict) -> float:
        """Return overlap ratio wrt the smaller of two bbox areas."""
        ax0, ay0, ax1, ay1 = a["x0"], a["y0"], a["x1"], a["y1"]
        bx0, by0, bx1, by1 = b["x0"], b["y0"], b["x1"], b["y1"]

        ix0, iy0 = max(ax0, bx0), max(ay0, by0)
        ix1, iy1 = min(ax1, bx1), min(ay1, by1)
        if ix1 <= ix0 or iy1 <= iy0:
            return 0.0

        inter = (ix1 - ix0) * (iy1 - iy0)
        area_a = max(0.0, (ax1 - ax0) * (ay1 - ay0))
        area_b = max(0.0, (bx1 - bx0) * (by1 - by0))
        base = min(area_a, area_b)
        if base <= 0.0:
            return 0.0
        return inter / base

    def _build_merged_blocks(self) -> None:
        """Merge current text-layer blocks with Pix2Text equation blocks."""
        line_blocks = []
        for idx, (text, bbox, page_num) in enumerate(self.line_info):
            line_blocks.append({
                "id": f"line-{idx}",
                "source": "current_layer",
                "page_num": page_num,
                "bbox": {"x0": bbox[0], "y0": bbox[1], "x1": bbox[2], "y1": bbox[3]},
                "content_type": "text",
                "text": text,
                "latex": None,
                "mathml": None,
                "confidence": None,
            })

        equation_blocks = []
        replaced_line_ids = set()
        for eq in self.pix2text_equations:
            eq_page = int(eq.get("page", 0))
            eq_bbox = eq.get("bbox") or {}
            if not all(k in eq_bbox for k in ("x0", "y0", "x1", "y1")):
                continue

            best_line = None
            best_overlap = 0.0
            for lb in line_blocks:
                if lb["page_num"] != eq_page:
                    continue
                if not self._is_likely_equation(lb["text"]):
                    continue
                overlap = self._bbox_overlap_ratio(lb["bbox"], eq_bbox)
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_line = lb

            if best_line is not None and best_overlap >= 0.40:
                replaced_line_ids.add(best_line["id"])

            equation_blocks.append({
                "id": f"pix2text-eq-{eq.get('index', len(equation_blocks))}",
                "source": "pix2text",
                "page_num": eq_page,
                "bbox": {
                    "x0": float(eq_bbox["x0"]),
                    "y0": float(eq_bbox["y0"]),
                    "x1": float(eq_bbox["x1"]),
                    "y1": float(eq_bbox["y1"]),
                },
                "content_type": "display_equation",
                "text": eq.get("text") or eq.get("latex") or "",
                "latex": eq.get("latex"),
                "mathml": eq.get("mathml"),
                "confidence": eq.get("confidence"),
            })

        merged = [lb for lb in line_blocks if lb["id"] not in replaced_line_ids] + equation_blocks
        merged.sort(key=lambda b: (b["page_num"], b["bbox"]["y0"], b["bbox"]["x0"]))

        for order, block in enumerate(merged):
            block["reading_order"] = order

        self.merged_blocks = merged
        self.merge_summary = {
            "line_blocks": len(line_blocks),
            "pix2text_equations": len(equation_blocks),
            "replaced_equation_like_lines": len(replaced_line_ids),
            "merged_blocks": len(merged),
        }

    def _collect_statistics(self, doc: fitz.Document) -> Dict:
        """
        Collect document statistics.

        Figure count:   GROBID <figure> elements (deduplicated by number,
                        filtered to only labelled figures).
        Table count:    Camelot-extracted table count (actual page tables).
        Equation count: GROBID <formula type="display"> elements (inline math
                        excluded); only numbered equations contribute to count.
        """
        if self.grobid_figures:
            total_figures = len(self.grobid_figures)
        else:
            figure_nums = {
                int(m.group(1))
                for m in re.finditer(r"(?:Figure|Fig\.?)\s+(\d+)", self.full_text, re.IGNORECASE)
            }
            total_figures = len(figure_nums)

        total_tables = self.total_tables_count
        total_equations = len([eq for eq in self._grobid_equations if eq.get("number") is not None])
        total_equations_pix2text = len(self.pix2text_equations)
        total_equations_merged = sum(1 for b in self.merged_blocks if b.get("content_type") == "display_equation")
        total_images = sum(len(doc[p].get_images(full=True)) for p in range(len(doc)))

        # Convert Camelot DataFrames to a JSON-serialisable format for the frontend.
        camelot_tables_serialised = []
        for t in self.extracted_tables:
            df = t.get("dataframe")
            rows = df.values.tolist() if df is not None else []
            rows_str = [[str(cell) for cell in row] for row in rows]
            camelot_tables_serialised.append({
                "index":   t["index"],
                "type":    "table",
                "label":   f"TABLE {t['index'] + 1}",
                "description": "",
                "page":    int(t["page"]) - 1,   # convert to 0-indexed to match GROBID convention
                "rows":    rows_str,
            })

        return {
            "total_words":      len(self.full_text.split()),
            "total_pages":      len(doc),
            "total_figures":    total_figures,
            "total_tables":     total_tables,
            "total_equations":  total_equations,
            "total_equations_pix2text": total_equations_pix2text,
            "total_equations_merged": total_equations_merged,
            "total_images":     total_images,
            "grobid_figures":   self.grobid_figures,
            "extracted_tables": camelot_tables_serialised,
            "grobid_equations": [
                {"index": eq["index"], "text": eq["text"][:100], "number": eq["number"], "page": eq["page"]}
                for eq in self._grobid_equations
            ],
            "pix2text_equations": [
                {
                    "index": eq.get("index"),
                    "text": str(eq.get("text") or "")[:120],
                    "latex": str(eq.get("latex") or "")[:120],
                    "page": eq.get("page"),
                    "confidence": eq.get("confidence"),
                }
                for eq in self.pix2text_equations
            ],
            "merge_summary": self.merge_summary,
            "pipeline_status": self.pipeline_status,
            # Unified section-detection results (single source of truth for
            # which sections are present in the paper).
            "detected_sections": [
                s for s, info in self.section_detection.items() if info.get("found")
            ],
            "section_detection": [
                {
                    "section":          s,
                    "found":            bool(info.get("found")),
                    "matched_heading":  info.get("matched_heading"),
                    "page":             info.get("page"),
                }
                for s, info in self.section_detection.items()
            ],
        }

    # =========================================================================
    # PUBLIC API
    # =========================================================================

    def detect_errors(
        self,
        pdf_path: str,
        required_sections: Optional[List[str]] = None,
        progress_callback=None,
    ) -> Tuple[List[ErrorInstance], fitz.Document, Dict]:
        """Open PDF, extract text and tables, run all checks, return errors + doc + stats."""
        doc = fitz.open(pdf_path)

        def _run_current_layer() -> None:
            try:
                # GROBID must run before _extract_all_text because it populates _tei_root.
                self._extract_with_grobid(pdf_path)
                self._extract_all_text(doc)
                self._extract_tables(pdf_path)
                self.pipeline_status["current_layer"] = {
                    "success": True,
                    "message": "Current extraction layer complete",
                }
            except Exception as exc:
                print(f"[CURRENT LAYER] Error: {exc}")
                # Preserve baseline fallback so checks can continue.
                self.full_text = ""
                self.page_texts = []
                self.line_info = []
                self.line_offsets = []
                self._extract_text_via_pymupdf(doc)
                self.pipeline_status["current_layer"] = {
                    "success": False,
                    "message": f"Current extraction layer failed: {exc}",
                }

        _run_current_layer()
        if progress_callback:
            progress_callback("grobid_parsing", "Parsing Document")
        self.pipeline_status["pix2text"] = {
            "enabled": False,
            "success": False,
            "message": "Pix2Text equation extraction disabled",
            "count": 0,
        }

        citations = self._extract_citations_from_pdf(doc)
        self.reference_analysis = self.analyze_references(citations)
        self.raw_citations = citations
        if progress_callback:
            progress_callback("reference_analysis", "Reference Analysis")

        try:
            self._build_merged_blocks()
            self.pipeline_status["merge"] = {"success": True, "message": "Merge complete"}
        except Exception as exc:
            print(f"[MERGE] Error: {exc}")
            self.merged_blocks = []
            self.merge_summary = {}
            self.pipeline_status["merge"] = {"success": False, "message": f"Merge failed: {exc}"}

        # Unified section detection (runs once, used by required-sections check
        # and surfaced in statistics for the UI).
        self._detect_all_sections(doc)

        statistics = self._collect_statistics(doc)
        errors = self._run_document_checks(doc, progress_callback=progress_callback)

        # Format-driven required-sections check (optional, caller-supplied list)
        if required_sections:
            errors.extend(self._check_required_sections(required_sections))
            if progress_callback:
                progress_callback("section_check", "Section Existence")

        # Filter out errors from pages before start_page — BUT preserve
        # document-level errors (missing sections, missing metadata) that are
        # not tied to a specific page.  Without this exemption, selecting
        # start_page > 1 would silently hide "Index Terms missing" etc.
        if self.start_page_0 > 0:
            DOCUMENT_LEVEL_TYPES = {"missing_required_section", "metadata_incomplete"}
            errors = [
                e for e in errors
                if e.error_type in DOCUMENT_LEVEL_TYPES
                or e.page_num >= self.start_page_0
            ]

        return errors, doc, statistics

    def export_extracted_data(self) -> Dict:
        """Export raw extracted data for external analysis."""
        return {
            "full_text":        self.full_text,
            "total_characters": len(self.full_text),
            "page_texts":       self.page_texts,
            "total_pages":      len(self.page_texts),
            "line_count":       len(self.line_info),
            "pix2text_equations": self.pix2text_equations,
            "merged_blocks": self.merged_blocks,
            "merge_summary": self.merge_summary,
            "pipeline_status": self.pipeline_status,
            "lines": [
                {
                    "text":     text,
                    "page_num": page_num,
                    "bbox":     {"x0": bbox[0], "y0": bbox[1], "x1": bbox[2], "y1": bbox[3]},
                }
                for text, bbox, page_num in self.line_info
            ],
        }

    # =========================================================================
    # ORCHESTRATOR
    # =========================================================================

    def _run_document_checks(self, doc: fitz.Document, progress_callback=None) -> List[ErrorInstance]:
        """Run all compliance and formatting checks, optionally firing progress_callback."""
        errors = []

        def emit(key: str, name: str) -> None:
            if progress_callback:
                progress_callback(key, name)

        # Metadata Completeness (25) — GROBID header model
        errors.extend(self._check_metadata_completeness())
        emit("metadata_completeness", "Metadata Completeness")

        # Structure — Roman numeral headings
        errors.extend(self._check_roman_numeral_headings())
        emit("roman_numeral_headings", "Roman Numeral Headings")

        # Numbering — label formats (6–8)
        errors.extend(self._check_figure_numbering())
        errors.extend(self._check_table_numbering())
        errors.extend(self._check_equation_numbering())
        emit("label_formats", "Figure & Table Labels")

        # Numbering — sequential checks (21–23)
        # errors.extend(self._check_figure_sequential_numbering())
        # errors.extend(self._check_table_sequential_numbering())
        errors.extend(self._check_reference_sequential_numbering())
        emit("sequential_numbering", "Sequential Numbering")

        # Formatting — caption placement (19–20)
        errors.extend(self._check_caption_placement(doc))
        emit("caption_placement", "Caption Placement")

        # References — URL & DOI validity (24)
        errors.extend(self._check_url_doi_validity())
        emit("url_doi_validity", "URL & DOI Validity")

        # Writing — style checks (12, 15–17)
        errors.extend(self._check_repeated_words())
        errors.extend(self._check_et_al_formatting())
        errors.extend(self._check_first_person_pronouns())
        errors.extend(self._check_references_numbered())
        emit("writing_style", "Writing Style")

        # Writing — punctuation & spacing (26, 28–33)
        errors.extend(self._check_space_before_punctuation_marks())
        errors.extend(self._check_double_punctuation())
        errors.extend(self._check_missing_space_after_punctuation_marks())
        errors.extend(self._check_comma_before_parenthesis())
        errors.extend(self._check_punctuation_inside_citation())
        errors.extend(self._check_multiple_spaces_between_words())
        errors.extend(self._check_space_inside_brackets())
        emit("punctuation_checks", "Punctuation & Spacing")

        return errors

    # =========================================================================
    # CORE HELPER — emits one ErrorInstance per regex match per line
    # =========================================================================

    def _find_all_occurrences(
        self,
        pattern: re.Pattern,
        check_id: int,
        check_name: str,
        error_type: str,
        description_fn,
        line_filter=None,
        start_after_keyword: Optional[str] = None,
        stop_at_keyword: Optional[str] = None,
    ) -> List[ErrorInstance]:
        errors = []
        active = start_after_keyword is None

        for line_text, line_bbox, page_num in self.line_info:
            if not active:
                if re.search(start_after_keyword, line_text, re.IGNORECASE):
                    active = True
                continue

            if stop_at_keyword and re.search(stop_at_keyword, line_text, re.IGNORECASE):
                break

            if line_filter and not line_filter(line_text):
                continue

            for match in pattern.finditer(line_text):
                errors.append(ErrorInstance(
                    check_id=check_id,
                    check_name=check_name,
                    description=description_fn(match, line_text),
                    page_num=page_num,
                    text=match.group(),
                    bbox=self._calculate_match_bbox(line_text, match, line_bbox),
                    error_type=error_type,
                ))

        return errors

    # =========================================================================
    # CHECK #25 — METADATA COMPLETENESS  (GROBID header model)
    # =========================================================================

    def _check_metadata_completeness(self) -> List[ErrorInstance]:
        """
        Verify that GROBID's header model successfully extracted all three
        essential metadata fields: title, at least one author, and a date.

        GROBID's header and citation models are its most mature features, so
        a missing or empty field almost always means the field is genuinely
        absent from the PDF rather than a GROBID extraction failure.

        One ErrorInstance is emitted per missing field, anchored to the top
        of the first page so the annotation appears near the paper header.
        """
        # Use the first line as a generic anchor for page-1 annotations.
        anchor_text, anchor_bbox, anchor_page = (
            self.line_info[0] if self.line_info else ("", (0, 0, 200, 20), 0)
        )

        meta = self._grobid_metadata
        errors = []

        if not meta:
            # GROBID was unavailable; skip the check entirely.
            return errors

        if not meta.get("title"):
            errors.append(ErrorInstance(
                check_id=25,
                check_name="Metadata Incomplete: Title Missing",
                description=(
                    "GROBID could not extract a paper title from the document header. "
                    "Ensure the title is present and clearly formatted on the first page."
                ),
                page_num=anchor_page,
                text="[Title not found in header]",
                bbox=anchor_bbox,
                error_type="metadata_incomplete",
            ))
        else:
            print(f"[METADATA] Title OK: \"{meta['title'][:60]}\"")

        if not meta.get("authors"):
            errors.append(ErrorInstance(
                check_id=25,
                check_name="Metadata Incomplete: Author(s) Missing",
                description=(
                    "GROBID could not extract any author names from the document header. "
                    "Ensure author names are present and consistently formatted."
                ),
                page_num=anchor_page,
                text="[No authors found in header]",
                bbox=anchor_bbox,
                error_type="metadata_incomplete",
            ))
        else:
            print(f"[METADATA] Authors OK: {meta['authors'][:3]}")

        if not meta.get("date"):
            errors.append(ErrorInstance(
                check_id=25,
                check_name="Metadata Incomplete: Publication Date Missing",
                description=(
                    "GROBID could not extract a publication date from the document. "
                    "IEEE papers should include the submission or publication year."
                ),
                page_num=anchor_page,
                text="[Publication date not found]",
                bbox=anchor_bbox,
                error_type="metadata_incomplete",
            ))
        else:
            print(f"[METADATA] Date OK: {meta['date']}")

        return errors

    # =========================================================================
    # UNIFIED SECTION DETECTION
    # ─────────────────────────────────────────────────────────────────────────
    # A single, high-accuracy pass that decides whether each canonical section
    # in ALL_SECTIONS is present in the document.  Used by:
    #   • _check_required_sections() – the only section-existence check
    #   • statistics["detected_sections"] / ["section_detection"] for the UI
    # =========================================================================

    # Strip leading Roman/Arabic numbering + optional letter prefix (e.g. "A.")
    # so that "III. RELATED WORK" normalises to "related work".
    _HEADING_PREFIX_RE = re.compile(
        r"^\s*(?:(?:[IVXLCDMivxlcdm]+|\d+|[A-Za-z])[\.\)\-:]\s+)+"
    )

    @staticmethod
    def _normalize_heading(text: str) -> str:
        t = (text or "").strip().lower()
        t = PDFErrorDetector._HEADING_PREFIX_RE.sub("", t)
        # Collapse whitespace
        t = re.sub(r"\s+", " ", t).strip()
        # Strip trailing punctuation that sometimes hangs off headings
        t = re.sub(r"[\.\:\;\,\s]+$", "", t)
        return t

    # --- PyMuPDF heading extraction helpers ---------------------------------

    # Lines longer than this are almost never a heading — skip early.
    _HEADING_MAX_CHARS = 120
    # Minimum characters to consider a heading (filters out noise like "1.").
    _HEADING_MIN_CHARS = 3

    def _extract_pymupdf_headings(
        self, doc: "fitz.Document"
    ) -> List[Dict]:
        """
        Extract heading-like lines from the PDF using PyMuPDF font information.

        A line is considered a heading candidate when TWO of these hold:
          • All of its non-trivial spans are bold, OR
          • Its dominant font size is materially larger than the body
            font size (>= body_size * 1.08), OR
          • It is ALL-CAPS, short, and stands alone (no sentence-ending
            punctuation).

        Returns a list of dicts: {text, normalized, page, bbox, size, bold}.
        """
        # ── Pass 1: collect every line's dominant size/bold to infer body size.
        line_records: List[Dict] = []
        size_hist: "Dict[float, int]" = {}

        for page_num in range(len(doc)):
            if page_num < self.start_page_0:
                continue
            page = doc[page_num]
            try:
                blocks = page.get_text("dict").get("blocks", [])
            except Exception:
                continue

            for block in blocks:
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    spans = [
                        s for s in line.get("spans", [])
                        if s.get("text", "").strip()
                    ]
                    if not spans:
                        continue

                    text = "".join(s["text"] for s in spans).strip()
                    text = re.sub(r"\s+", " ", text)
                    if not text:
                        continue

                    # Dominant size = size of the longest span (in chars).
                    dominant = max(spans, key=lambda s: len(s.get("text", "")))
                    size = round(float(dominant.get("size", 0.0)), 1)

                    # Bold if ALL non-trivial spans are bold (by flag or name).
                    def _is_bold(span):
                        flags = int(span.get("flags", 0) or 0)
                        name = (span.get("font", "") or "").lower()
                        return bool(flags & 16) or "bold" in name or "black" in name
                    bold = all(_is_bold(s) for s in spans)

                    bbox = line.get("bbox") or (0, 0, 0, 0)

                    line_records.append({
                        "text": text,
                        "page": page_num,
                        "bbox": bbox,
                        "size": size,
                        "bold": bold,
                    })
                    # Only count body-ish line sizes (>= 6pt, < 30pt).
                    if 6.0 <= size < 30.0:
                        size_hist[size] = size_hist.get(size, 0) + 1

        if not line_records:
            return []

        # ── Infer body size: the MOST COMMON size across the document.
        body_size = max(size_hist.items(), key=lambda kv: kv[1])[0] if size_hist else 10.0
        heading_size_cutoff = body_size * 1.15

        # ── Pass 2: filter to heading candidates.
        headings: List[Dict] = []
        for rec in line_records:
            text = rec["text"]
            n = len(text)
            if n < self._HEADING_MIN_CHARS or n > self._HEADING_MAX_CHARS:
                continue
            # Skip obvious running-text: ends with sentence punctuation and
            # is not a numbered heading.
            if re.search(r"[\.\?!]\s*$", text) and not re.match(
                r"^\s*(?:[IVXLCDM]+|\d+|[A-Z])[\.\)]\s", text
            ):
                continue
            # Strip leading/trailing quotes that sometimes appear in captions.
            if text.startswith(("Figure", "Fig.", "Table")):
                continue

            is_large = rec["size"] >= heading_size_cutoff
            is_all_caps = text == text.upper() and any(c.isalpha() for c in text) and n <= 80

            if  is_large or is_all_caps:
                rec["normalized"] = self._normalize_heading(text)
                headings.append(rec)

        print(
            f"[PYMUPDF HEADINGS] body_size={body_size}pt, "
            f"cutoff={heading_size_cutoff:.2f}pt, "
            f"found {len(headings)} heading candidates"
        )
        return headings

    def _detect_all_sections(self, doc: Optional["fitz.Document"] = None) -> None:
        """
        Detect which canonical sections (ALL_SECTIONS) are present in the paper.

        Accuracy strategy (highest-confidence signal wins):
          1. GROBID structural tags:
               • <abstract>   → Abstract
               • <keywords>   → Index Terms
               • <listBibl>   → References (via self.raw_citations being non-empty)
          2. GROBID <div><head> elements matched against
             SECTION_DETECTION_KEYWORDS with word-boundary matching on a
             normalised heading string (numbers/letters stripped).
          3. PyMuPDF-based heading extraction (bold / larger-than-body /
             ALL-CAPS lines). Used whenever we have a doc handle — this is
             the robust fallback when GROBID fails or returns nothing.

        Crucially, we NEVER fall back to full-text regex matching: words
        like "performance" or "preliminary" appear routinely in body text
        and would produce false positives.

        Populates self.section_detection keyed by canonical section name.
        """
        detection: "OrderedDict[str, Dict]" = OrderedDict()

        # Pre-normalize GROBID headings once.
        grobid_heads: List[Tuple[str, str, Dict]] = [
            (self._normalize_heading(h["text"]), h["text"], h)
            for h in self._grobid_section_heads
        ]

        # If GROBID gave us few/no headings, enrich with PyMuPDF headings.
        # Threshold: <3 GROBID heads is a strong signal GROBID struggled.
        pymupdf_heads: List[Dict] = []
        if doc is not None and len(grobid_heads) < 3:
            try:
                pymupdf_heads = self._extract_pymupdf_headings(doc)
            except Exception as exc:
                print(f"[PYMUPDF HEADINGS] Error: {exc}")
                pymupdf_heads = []

        def match_in_grobid(keywords: List[str]):
            for kw in sorted(keywords, key=len, reverse=True):
                kw_re = re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE)
                for norm, original, head in grobid_heads:
                    if kw_re.search(norm):
                        return original, head.get("page")
            return None, None

        def match_in_pymupdf(keywords: List[str]):
            for kw in sorted(keywords, key=len, reverse=True):
                kw_re = re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE)
                for h in pymupdf_heads:
                    if kw_re.search(h["normalized"]):
                        return h["text"], h["page"]
            return None, None

        for section in ALL_SECTIONS:
            keywords = SECTION_DETECTION_KEYWORDS.get(section, [section.lower()])
            found = False
            matched_heading: Optional[str] = None
            page: Optional[int] = None

            # ── (1) Structural GROBID signals ───────────────────────────────
            if section == "Abstract" and self._grobid_has_abstract:
                found = True
                matched_heading = "Abstract (GROBID <abstract>)"
            elif section == "Index Terms" and self._grobid_has_keywords:
                found = True
                matched_heading = "Index Terms (GROBID <keywords>)"
            elif section == "References" and self.raw_citations:
                found = True
                matched_heading = f"References ({len(self.raw_citations)} parsed)"

            # ── (2) GROBID headings ─────────────────────────────────────────
            if not found and grobid_heads:
                matched_heading, page = match_in_grobid(keywords)
                found = matched_heading is not None

            # ── (3) PyMuPDF headings (bold / larger / all-caps) ─────────────
            if not found and pymupdf_heads:
                matched_heading, page = match_in_pymupdf(keywords)
                if matched_heading is not None:
                    found = True
                    matched_heading = f"{matched_heading} (PyMuPDF heading)"

            detection[section] = {
                "found": found,
                "matched_heading": matched_heading,
                "page": page,
            }

        self.section_detection = detection

        print("\n[DEBUG] Full Section Detection:")
        for section, info in detection.items():
            print(
                f"{section}: found={info['found']}, "
                f"matched='{info['matched_heading']}', "
                f"page={info['page']}"
            )

        present = [s for s, info in detection.items() if info["found"]]
        print(f"[SECTIONS] Detected ({len(present)}): {', '.join(present) or 'none'}")

    # =========================================================================
    # CHECK #4 — ROMAN NUMERAL SECTION HEADINGS
    # =========================================================================

    def _check_roman_numeral_headings(self) -> List[ErrorInstance]:
        """
        Flag every section heading that uses Arabic numerals instead of Roman numerals.

        Primary:  Iterate _grobid_section_heads — these are the actual <div><head>
                  elements GROBID parsed, so we only look at real headings, not
                  arbitrary lines that happen to start with a digit.
        Fallback: Original regex over line_info (when GROBID found no headings).
        """
        arabic_heading = re.compile(r"^(\d+)\.\s+([A-Z][a-zA-Z\s]{2,50})$")
        errors = []

        if self._grobid_section_heads:
            # ── GROBID path ──────────────────────────────────────────────────
            for head in self._grobid_section_heads:
                stripped = head["text"].strip()
                m = arabic_heading.match(stripped)
                if m and 2 <= len(stripped.split()) <= 8:
                    errors.append(ErrorInstance(
                        check_id=4,
                        check_name="Non-Roman Numeral Section Heading",
                        description=(
                            f"Section heading '{stripped}' uses Arabic numeral '{m.group(1)}'. "
                            "IEEE format requires Roman numerals in uppercase (e.g., 'I. INTRODUCTION')."
                        ),
                        page_num=head["page"],
                        text=stripped,
                        bbox=head["bbox"],
                        error_type="non_roman_heading",
                    ))
        else:
            # ── Fallback: original line_info scan ────────────────────────────
            for line_text, line_bbox, page_num in self.line_info:
                stripped = line_text.strip()
                m = arabic_heading.match(stripped)
                if m and 2 <= len(stripped.split()) <= 8:
                    errors.append(ErrorInstance(
                        check_id=4,
                        check_name="Non-Roman Numeral Section Heading",
                        description=(
                            f"Section heading '{stripped}' uses Arabic numeral '{m.group(1)}'. "
                            "IEEE format requires Roman numerals in uppercase (e.g., 'I. INTRODUCTION')."
                        ),
                        page_num=page_num,
                        text=stripped,
                        bbox=line_bbox,
                        error_type="non_roman_heading",
                    ))

        return errors

    # =========================================================================
    # CHECK #6 — IN-TEXT CITATION FORMAT [n]  (unchanged)
    # =========================================================================

    def _check_intext_citation_format(self) -> List[ErrorInstance]:
        errors = []
        errors.extend(self._find_all_occurrences(
            pattern=re.compile(r"\([A-Za-z]+(?:\s+et\s+al\.?)?,\s*\d{4}\)"),
            check_id=6,
            check_name="Non-IEEE Citation Format (APA Style)",
            error_type="non_ieee_citation",
            description_fn=lambda m, line: (
                f"Citation '{m.group()}' uses APA format. "
                "IEEE requires bracketed numeric citations like [1]."
            ),
        ))
        errors.extend(self._find_all_occurrences(
            pattern=re.compile(r"\([A-Za-z]+\s+\d+\)"),
            check_id=6,
            check_name="Non-IEEE Citation Format (MLA Style)",
            error_type="non_ieee_citation",
            description_fn=lambda m, line: (
                f"Citation '{m.group()}' uses MLA format. "
                "IEEE requires bracketed numeric citations like [1]."
            ),
        ))
        return errors

    # =========================================================================
    # CHECK #17 — REFERENCES NUMBERED [n]  (unchanged)
    # =========================================================================

    def _check_references_numbered(self) -> List[ErrorInstance]:
        non_ieee_ref = re.compile(r"^(\d+)\.\s+\S|^\((\d+)\)\s+[A-Z]")
        errors = []
        in_references = False

        for line_text, line_bbox, page_num in self.line_info:
            if re.search(r"\b(References|REFERENCES)\b", line_text):
                in_references = True
                continue
            if not in_references:
                continue

            stripped = line_text.strip()
            m = non_ieee_ref.match(stripped)
            if m:
                num = m.group(1) or m.group(2)
                errors.append(ErrorInstance(
                    check_id=17,
                    check_name="Reference Not in IEEE Bracketed Format",
                    description=(
                        f"Reference entry starts with '{num}.' or '({num})' instead of '[{num}]'. "
                        "IEEE requires references formatted as [1] Author, Title..."
                    ),
                    page_num=page_num,
                    text=stripped[:70],
                    bbox=line_bbox,
                    error_type="non_ieee_reference_format",
                ))

        return errors

    # =========================================================================
    # CHECK #6 — FIGURE NUMBERING  (unchanged)
    # =========================================================================

    def _check_figure_numbering(self) -> List[ErrorInstance]:
        errors = []
        errors.extend(self._find_all_occurrences(
            pattern=re.compile(r"\bFIGURE\s+\d+\b"),
            check_id=6,
            check_name="Figure Label All-Caps (Use 'Fig.' or 'Figure')",
            error_type="invalid_figure_label",
            description_fn=lambda m, line: (
                f"'{m.group()}' uses all-caps 'FIGURE'. "
                "IEEE convention is 'Fig. N' or 'Figure N'."
            ),
        ))
        errors.extend(self._find_all_occurrences(
            pattern=re.compile(r"\bfig\s+\d+\b", re.IGNORECASE),
            check_id=6,
            check_name="Figure Abbreviation Missing Period (Use 'Fig.')",
            error_type="invalid_figure_label",
            description_fn=lambda m, line: (
                f"'{m.group()}' is missing the period after 'Fig'. "
                "IEEE convention is 'Fig. N' (with period)."
            ),
            line_filter=lambda t: not re.search(r"\bFig\.\s*\d+\b", t),
        ))
        return errors

    # =========================================================================
    # CHECK #7 — TABLE NUMBERING  (unchanged)
    # =========================================================================

    def _check_table_numbering(self) -> List[ErrorInstance]:
        errors = []
        errors.extend(self._find_all_occurrences(
            pattern=re.compile(r"\bTABLE\s+\d+\b"),
            check_id=7,
            check_name="Table Uses Arabic Numeral (Use Roman Numeral)",
            error_type="invalid_table_numbering",
            description_fn=lambda m, line: (
                f"'{m.group()}' uses an Arabic numeral. "
                "IEEE requires Roman numerals in uppercase, e.g., 'TABLE I', 'TABLE II'."
            ),
        ))
        errors.extend(self._find_all_occurrences(
            pattern=re.compile(r"\b[Tt]able\s+[\dIVXLCDMivxlcdm]+\b"),
            check_id=7,
            check_name="Table Label Not in Uppercase (Use 'TABLE')",
            error_type="invalid_table_numbering",
            description_fn=lambda m, line: (
                f"'{m.group()}' is not fully uppercase. "
                "IEEE format requires 'TABLE' in all-caps, e.g., 'TABLE I'."
            ),
            line_filter=lambda t: not re.search(r"\bTABLE\s+[IVXLCDM]+\b", t),
        ))
        return errors

    # =========================================================================
    # CHECK #8 — EQUATION NUMBERING
    # =========================================================================

    def _check_equation_numbering(self) -> List[ErrorInstance]:
        """
        Verify equation numbering format and sequence.

        Primary path: use GROBID's already-parsed _grobid_equations (display
        equations only, with numbers extracted from <label> or regex).
        Fallback: heuristic scan of line_info (original approach).
        """
        errors = []
        eq_numbers: List[int] = []
        eq_locations: Dict[int, Tuple] = {}

        if self._grobid_equations:
            for eq in self._grobid_equations:
                if eq.get("number") is not None:
                    eq_num = eq["number"]
                    eq_numbers.append(eq_num)
                    if eq_num not in eq_locations:
                        eq_locations[eq_num] = (eq["text"], eq["bbox"], eq["page"])
                else:
                    # Display equation with no recognised number
                    bare = re.search(r"(?<!\()\b(\d+)\b\s*$", eq["text"])
                    if bare:
                        errors.append(ErrorInstance(
                            check_id=8,
                            check_name="Equation Number Not in Parentheses",
                            description=(
                                f"Equation number '{bare.group(1)}' is not wrapped in parentheses. "
                                "IEEE format requires (1), (2), etc."
                            ),
                            page_num=eq["page"],
                            text=eq["text"].strip()[-70:],
                            bbox=eq["bbox"],
                            error_type="equation_numbering",
                        ))
        else:
            for line_text, line_bbox, page_num in self.line_info:
                if not self._is_likely_equation(line_text):
                    continue

                valid_match = re.search(r"\((\d+)\)\s*$", line_text)
                if valid_match:
                    eq_num = int(valid_match.group(1))
                    eq_numbers.append(eq_num)
                    if eq_num not in eq_locations:
                        eq_locations[eq_num] = (line_text, line_bbox, page_num)
                else:
                    bare = re.search(r"(?<!\()\b(\d+)\b\s*$", line_text)
                    if bare:
                        errors.append(ErrorInstance(
                            check_id=8,
                            check_name="Equation Number Not in Parentheses",
                            description=(
                                f"Equation number '{bare.group(1)}' is not wrapped in parentheses. "
                                "IEEE format requires (1), (2), etc."
                            ),
                            page_num=page_num,
                            text=line_text.strip()[-70:] if len(line_text.strip()) > 70 else line_text.strip(),
                            bbox=line_bbox,
                            error_type="equation_numbering",
                        ))

        if len(eq_numbers) >= 2:
            unique = sorted(set(eq_numbers))
            for i in range(len(unique) - 1):
                if unique[i + 1] != unique[i] + 1:
                    out_num = unique[i + 1]
                    if out_num in eq_locations:
                        line_text, line_bbox, page_num = eq_locations[out_num]
                        errors.append(ErrorInstance(
                            check_id=8,
                            check_name="Non-Sequential Equation Numbering",
                            description=(
                                f"Equation ({out_num}) does not follow ({unique[i]}) sequentially. "
                                "IEEE equations must be numbered consecutively."
                            ),
                            page_num=page_num,
                            text=f"({out_num})",
                            bbox=line_bbox,
                            error_type="equation_numbering",
                        ))

        return errors

    # =========================================================================
    # CHECK #21 — FIGURE SEQUENTIAL NUMBERING (GROBID)
    # =========================================================================

    def _check_figure_sequential_numbering(self) -> List[ErrorInstance]:
        """
        Verify figures are numbered sequentially (1, 2, 3, ...) with no gaps.
        Uses GROBID figure entries (already deduplicated with a 'number' field)
        for accurate detection; falls back to regex on raw text.
        """
        errors = []
        fig_numbers = []

        if self._grobid_figure_entries:
            for fig in self._grobid_figure_entries:
                num = fig.get("number")
                if num is not None:
                    fig_numbers.append({"num": num, "entry": fig})
        else:
            for line_text, line_bbox, page_num in self.line_info:
                m = re.search(r'(?:Fig\.?|Figure)\s+(\d+)', line_text, re.IGNORECASE)
                if m:
                    fig_numbers.append({
                        "num": int(m.group(1)),
                        "entry": {"page": page_num, "bbox": line_bbox, "caption": line_text.strip()},
                    })

        seen = {}
        for fn in fig_numbers:
            if fn["num"] not in seen:
                seen[fn["num"]] = fn

        if not seen:
            return errors

        sorted_nums = sorted(seen.keys())
        for i, num in enumerate(sorted_nums):
            expected = i + 1
            if num != expected:
                entry = seen[num]
                errors.append(ErrorInstance(
                    check_id=21,
                    check_name="Non-Sequential Figure Numbering",
                    description=(
                        f"Figure {num} found but expected Figure {expected}. "
                        "Figures must be numbered sequentially with no gaps."
                    ),
                    page_num=entry["entry"]["page"],
                    text=entry["entry"].get("caption", "")[:70],
                    bbox=entry["entry"]["bbox"],
                    error_type="figure_numbering_sequence",
                ))

        return errors

    # =========================================================================
    # CHECK #22 — TABLE SEQUENTIAL NUMBERING (GROBID)
    # =========================================================================

    _ROMAN_TO_INT = {
        'I': 1, 'II': 2, 'III': 3, 'IV': 4, 'V': 5, 'VI': 6,
        'VII': 7, 'VIII': 8, 'IX': 9, 'X': 10, 'XI': 11, 'XII': 12,
        'XIII': 13, 'XIV': 14, 'XV': 15, 'XVI': 16, 'XVII': 17,
        'XVIII': 18, 'XIX': 19, 'XX': 20,
    }

    def _check_table_sequential_numbering(self) -> List[ErrorInstance]:
        """
        Verify tables are numbered sequentially (I, II, III or 1, 2, 3).
        Uses GROBID table entries for accurate detection; falls back to regex.
        """
        errors = []
        tbl_numbers = []

        if self._grobid_table_entries:
            for tbl in self._grobid_table_entries:
                text = tbl.get("label", "") or tbl.get("caption", "")
                m = re.search(r'TABLE\s+([IVXLCDM]+|\d+)', text, re.IGNORECASE)
                if m:
                    raw = m.group(1).upper()
                    num = self._ROMAN_TO_INT.get(raw)
                    if num is None:
                        try:
                            num = int(raw)
                        except ValueError:
                            continue
                    tbl_numbers.append({"num": num, "entry": tbl})
        else:
            for line_text, line_bbox, page_num in self.line_info:
                m = re.search(r'TABLE\s+([IVXLCDM]+|\d+)', line_text, re.IGNORECASE)
                if m:
                    raw = m.group(1).upper()
                    num = self._ROMAN_TO_INT.get(raw)
                    if num is None:
                        try:
                            num = int(raw)
                        except ValueError:
                            continue
                    tbl_numbers.append({
                        "num": num,
                        "entry": {"page": page_num, "bbox": line_bbox, "caption": line_text.strip()},
                    })

        seen = {}
        for tn in tbl_numbers:
            if tn["num"] not in seen:
                seen[tn["num"]] = tn

        if not seen:
            return errors

        sorted_nums = sorted(seen.keys())
        for i, num in enumerate(sorted_nums):
            expected = i + 1
            if num != expected:
                entry = seen[num]
                errors.append(ErrorInstance(
                    check_id=22,
                    check_name="Non-Sequential Table Numbering",
                    description=(
                        f"Table {num} found but expected Table {expected}. "
                        "Tables must be numbered sequentially with no gaps."
                    ),
                    page_num=entry["entry"]["page"],
                    text=entry["entry"].get("caption", "")[:70],
                    bbox=entry["entry"]["bbox"],
                    error_type="table_numbering_sequence",
                ))

        return errors

    # =========================================================================
    # CHECK #23 — REFERENCE SEQUENTIAL NUMBERING
    # =========================================================================

    def _check_reference_sequential_numbering(self) -> List[ErrorInstance]:
        """
        Verify that references in the reference section are numbered
        sequentially as [1], [2], [3], ... with no gaps.
        """
        errors = []
        ref_numbers = []
        in_references = False

        for line_text, line_bbox, page_num in self.line_info:
            if re.search(r"\b(References|REFERENCES)\b", line_text):
                in_references = True
                continue

            if not in_references:
                continue

            m = re.match(r"^\s*\[(\d+)\]", line_text)
            if m:
                ref_numbers.append({
                    "num": int(m.group(1)),
                    "text": line_text.strip(),
                    "bbox": line_bbox,
                    "page": page_num,
                })

        seen = {}
        for rn in ref_numbers:
            if rn["num"] not in seen:
                seen[rn["num"]] = rn

        if not seen:
            return errors

        sorted_nums = sorted(seen.keys())

        for i in range(1, len(sorted_nums)):
            prev = sorted_nums[i - 1]
            curr = sorted_nums[i]

            if curr != prev + 1:
                entry = seen[curr]
                errors.append(ErrorInstance(
                    check_id=23,
                    check_name="Non-Sequential Reference Numbering",
                    description=(
                        f"Reference [{curr}] found after [{prev}]. "
                        f"Expected [{prev + 1}]. References must be sequential."
                    ),
                    page_num=entry["page"],
                    text=entry["text"][:70],
                    bbox=entry["bbox"],
                    error_type="reference_numbering_sequence",

                ))

        return errors
    # =========================================================================
    # CHECK #24 — URL AND DOI VALIDITY
    # =========================================================================

    def _check_url_doi_validity(self) -> List[ErrorInstance]:
        """
        Check that URLs and DOIs in the document are well-formed and unbroken.
        Detects truncated URLs, malformed DOIs, and URLs with trailing punctuation
        that suggests they were broken during copy/paste.
        """
        errors = []

        url_re = re.compile(r'https?://[^\s\]\)>]+')
        doi_re = re.compile(r'\b(10\.\d{4,}/[^\s\]\)>,]+)')

        for line_text, line_bbox, page_num in self.line_info:
            # Check URLs
            for match in url_re.finditer(line_text):
                url = match.group()
                clean = url.rstrip('.,;:)')

                if url != clean and len(url) - len(clean) > 0:
                    errors.append(ErrorInstance(
                        check_id=24,
                        check_name="Possibly Broken URL",
                        description=(
                            f"URL '{url[:80]}' ends with punctuation that may be part "
                            "of surrounding text rather than the URL itself."
                        ),
                        page_num=page_num,
                        text=url[:70],
                        bbox=self._calculate_match_bbox(line_text, match, line_bbox),
                        error_type="broken_url",
                    ))

                parsed = urllib.parse.urlparse(clean)
                if not parsed.netloc or '.' not in parsed.netloc:
                    errors.append(ErrorInstance(
                        check_id=24,
                        check_name="Malformed URL",
                        description=(
                            f"URL '{clean[:80]}' appears malformed — "
                            "missing valid domain name."
                        ),
                        page_num=page_num,
                        text=clean[:70],
                        bbox=self._calculate_match_bbox(line_text, match, line_bbox),
                        error_type="broken_url",
                    ))

            # Check DOIs
            for match in doi_re.finditer(line_text):
                doi = match.group(1)
                clean_doi = doi.rstrip('.,;:)')

                if not re.match(r'^10\.\d{4,}/\S+$', clean_doi) or len(clean_doi) < 10:
                    errors.append(ErrorInstance(
                        check_id=24,
                        check_name="Malformed DOI",
                        description=(
                            f"DOI '{clean_doi[:80]}' appears incomplete or malformed. "
                            "Expected format: 10.XXXX/identifier"
                        ),
                        page_num=page_num,
                        text=clean_doi[:70],
                        bbox=self._calculate_match_bbox(line_text, match, line_bbox),
                        error_type="broken_doi",
                    ))

        return errors

    # =========================================================================
    # TYPOGRAPHY & FORMATTING CHECKS  (unchanged)
    # =========================================================================

    def _check_double_spaces(self) -> List[ErrorInstance]:
        return self._find_all_occurrences(
            pattern=re.compile(r"  +"),
            check_id=9,
            check_name="Multiple Consecutive Spaces",
            error_type="spacing_error",
            description_fn=lambda m, _: f"Found {len(m.group())} consecutive spaces — should be single space",
        )

    def _check_space_before_punctuation(self) -> List[ErrorInstance]:
        errors = []
        pattern = re.compile(r"\s+([.,;:])")
        for line_text, line_bbox, page_num in self.line_info:
            for match in pattern.finditer(line_text):
                if match.start() > 0 and line_text[match.start() - 1].isdigit():
                    continue
                errors.append(ErrorInstance(
                    check_id=10,
                    check_name="Space Before Punctuation",
                    description="Remove space before comma, period, semicolon, or colon",
                    page_num=page_num,
                    text=match.group(),
                    bbox=self._calculate_match_bbox(line_text, match, line_bbox),
                    error_type="punctuation_spacing",
                ))
        return errors

    def _check_missing_space_after_punctuation(self) -> List[ErrorInstance]:
        errors = []
        errors.extend(self._find_all_occurrences(
            pattern=re.compile(r",(?=[A-Za-z])"),
            check_id=11,
            check_name="Missing Space After Comma",
            error_type="punctuation_spacing",
            description_fn=lambda m, _: "Comma should be followed by a space",
        ))
        errors.extend(self._find_all_occurrences(
            pattern=re.compile(r"\.(?=[A-Z][a-z])"),
            check_id=11,
            check_name="Missing Space After Period",
            error_type="punctuation_spacing",
            description_fn=lambda m, _: "Period should be followed by a space",
        ))
        errors.extend(self._find_all_occurrences(
            pattern=re.compile(r";(?=[A-Za-z])"),
            check_id=11,
            check_name="Missing Space After Semicolon",
            error_type="punctuation_spacing",
            description_fn=lambda m, _: "Semicolon should be followed by a space",
        ))
        return errors

    def _check_repeated_words(self) -> List[ErrorInstance]:
        errors = []
        pattern = re.compile(r"\b(\w+)\s+\1\b", re.IGNORECASE)
        for line_text, line_bbox, page_num in self.line_info:
            for match in pattern.finditer(line_text):
                word = match.group(1).lower()
                if word in {"very", "long", "far", "many", "much"} or word.isdigit():
                    continue
                errors.append(ErrorInstance(
                    check_id=12,
                    check_name="Repeated Word",
                    description=f"Word '{match.group(1)}' appears twice consecutively",
                    page_num=page_num,
                    text=match.group(),
                    bbox=self._calculate_match_bbox(line_text, match, line_bbox),
                    error_type="repeated_word",
                ))
        return errors

    def _check_multiple_punctuation(self) -> List[ErrorInstance]:
        errors = []
        pattern = re.compile(r"([.!?])\1+")
        for line_text, line_bbox, page_num in self.line_info:
            for match in pattern.finditer(line_text):
                if match.group() == "...":
                    continue
                errors.append(ErrorInstance(
                    check_id=13,
                    check_name="Multiple Punctuation Marks",
                    description=f"Multiple consecutive punctuation '{match.group()}' inappropriate for academic writing",
                    page_num=page_num,
                    text=match.group(),
                    bbox=self._calculate_match_bbox(line_text, match, line_bbox),
                    error_type="punctuation_error",
                ))
        return errors

    def _check_trailing_spaces(self) -> List[ErrorInstance]:
        errors = []
        for line_text, line_bbox, page_num in self.line_info:
            if line_text and line_text != line_text.rstrip():
                trailing_count = len(line_text) - len(line_text.rstrip())
                errors.append(ErrorInstance(
                    check_id=14,
                    check_name="Trailing Whitespace",
                    description=f"Line has {trailing_count} trailing space(s) at the end",
                    page_num=page_num,
                    text=repr(line_text[-20:]) if len(line_text) > 20 else repr(line_text),
                    bbox=line_bbox,
                    error_type="whitespace_error",
                ))
        return errors

    def _check_et_al_formatting(self) -> List[ErrorInstance]:
        return self._find_all_occurrences(
            pattern=re.compile(r"\bet\s+al(?!\.)|et\.\s*al\.", re.IGNORECASE),
            check_id=15,
            check_name="Incorrect et al. Formatting",
            error_type="citation_format",
            description_fn=lambda m, _: "Should be 'et al.' (with period after 'al', not after 'et')",
        )

    # First-person pronouns. Capital "I" is matched on its own (lowercase "i"
    # is never a pronoun); everything else is case-insensitive so "We", "we",
    # "WE", "Our", "OUR" etc. all trigger.
    #
    # Reference-list author initials like "I. Smith" are suppressed via a
    # negative lookahead: \bI\b not followed by ". X..." (period + spaces +
    # capital letter), which is the canonical IEEE initial pattern.
    _FIRST_PERSON_RE = re.compile(
        r"(?-i:\bI\b)(?!\.\s*[A-Z])"                             # capital I only, not an author initial
        r"|\b(?:we|our|ours|ourselves|us|my|mine|myself|me)\b",  # others, case-insensitive
        re.IGNORECASE,
    )

    def _check_first_person_pronouns(self) -> List[ErrorInstance]:
        errors: List[ErrorInstance] = []
        for line_text, line_bbox, page_num in self.line_info:
            for match in self._FIRST_PERSON_RE.finditer(line_text):
                word = match.group(0)
                # Defensive: regex-flag fallback — reject a lowercase "i".
                if word == "i":
                    continue
                errors.append(ErrorInstance(
                    check_id=16,
                    check_name="First-Person Pronoun",
                    description=(
                        f"IEEE-style papers prefer impersonal tone over "
                        f"first-person pronouns like '{word}'"
                    ),
                    page_num=page_num,
                    text=word,
                    bbox=self._calculate_match_bbox(line_text, match, line_bbox),
                    error_type="writing_style",
                ))
        return errors

    # =========================================================================
    # PUNCTUATION & SPACING CHECKS (26, 28–33) — Writing section
    # =========================================================================

    _PUNCT_NAMES: Dict[str, str] = {
        ',': 'comma', '.': 'period', ';': 'semicolon',
        ':': 'colon', '!': 'exclamation mark', '?': 'question mark',
    }

    # Known abbreviation stems whose trailing period must not trigger a
    # "missing space after punctuation" alert.
    _ABBREV_STEMS_RE = re.compile(
        r'\b(?:et|al|vs|etc|fig|eq|no|vol|pp|ed|eds|dr|mr|mrs|ms|prof|'
        r'st|ave|blvd|rd|jr|sr|dept|univ|approx|est|ref|sec|ch|p)\.',
        re.IGNORECASE,
    )

    def _check_space_before_punctuation_marks(self) -> List[ErrorInstance]:
        """Flag a space immediately before , . ; : ! ? — e.g. 'hello , world'."""
        errors: List[ErrorInstance] = []
        pattern = re.compile(r'\s([,\.;:!?])')
        for line_text, line_bbox, page_num in self.line_info:
            for match in pattern.finditer(line_text):
                punct = match.group(1)
                pre = match.start()
                # Skip numeric context: "3 .14" or "page 5 ."  only when digit is right before space
                if pre > 0 and line_text[pre - 1].isdigit():
                    continue
                errors.append(ErrorInstance(
                    check_id=26,
                    check_name="Space Before Punctuation",
                    description=(
                        f"Unexpected space before "
                        f"{self._PUNCT_NAMES.get(punct, 'punctuation')} '{punct}' — remove the space"
                    ),
                    page_num=page_num,
                    text=match.group(),
                    bbox=self._calculate_match_bbox(line_text, match, line_bbox),
                    error_type="punctuation_spacing",
                ))
        return errors

    def _check_double_punctuation(self) -> List[ErrorInstance]:
        """Flag repeated or mixed terminal punctuation — e.g. 'end..' or 'what?!'."""
        errors: List[ErrorInstance] = []
        pattern = re.compile(r'[.!?]{2,}')
        for line_text, line_bbox, page_num in self.line_info:
            for match in pattern.finditer(line_text):
                # Ellipsis (exactly three dots) is an accepted typographic convention
                if match.group() == '...':
                    continue
                errors.append(ErrorInstance(
                    check_id=28,
                    check_name="Double Punctuation",
                    description=(
                        f"Repeated or mixed punctuation '{match.group()}' is "
                        f"inappropriate in academic writing"
                    ),
                    page_num=page_num,
                    text=match.group(),
                    bbox=self._calculate_match_bbox(line_text, match, line_bbox),
                    error_type="punctuation_error",
                ))
        return errors

    def _check_missing_space_after_punctuation_marks(self) -> List[ErrorInstance]:
        """Flag punctuation immediately followed by a letter, skipping decimals,
        single-char initials, common abbreviations, and URLs."""
        errors: List[ErrorInstance] = []
        # (?<![A-Z]) — skips abbreviation initials like U.S., I.E.
        # (?<!\d)    — skips decimals like 3.14 or version numbers 2.0
        pattern = re.compile(r'(?<![A-Z])(?<!\d)([.!?;:,])(?=[a-zA-Z])')
        for line_text, line_bbox, page_num in self.line_info:
            # Blank out URLs so they don't produce false positives
            clean = re.sub(r'https?://\S+', ' ' * len(re.search(r'https?://\S+', line_text).group())
                           if re.search(r'https?://\S+', line_text) else '', line_text)
            for match in pattern.finditer(clean):
                pos = match.start()
                punct = match.group(1)
                if punct == '.':
                    # Skip single-letter initials: "A." "B." etc.
                    if pos >= 1 and clean[pos - 1].isalpha() and (pos < 2 or not clean[pos - 2].isalpha()):
                        continue
                    # Skip known abbreviation stems
                    window = clean[max(0, pos - 12): pos + 2]
                    if self._ABBREV_STEMS_RE.search(window):
                        continue
                errors.append(ErrorInstance(
                    check_id=29,
                    check_name="Missing Space After Punctuation",
                    description=(
                        f"Add a space after "
                        f"{self._PUNCT_NAMES.get(punct, 'punctuation')} '{punct}'"
                    ),
                    page_num=page_num,
                    text=clean[max(0, pos - 2): pos + 4],
                    bbox=self._calculate_match_bbox(clean, match, line_bbox),
                    error_type="punctuation_spacing",
                ))
        return errors

    def _check_comma_before_parenthesis(self) -> List[ErrorInstance]:
        """Flag a comma placed directly before an opening parenthesis: 'result ,(see'."""
        return self._find_all_occurrences(
            pattern=re.compile(r',\s*\('),
            check_id=30,
            check_name="Comma Before Parenthesis",
            error_type="punctuation_error",
            description_fn=lambda m, _: (
                "Comma should not appear directly before an opening parenthesis"
            ),
        )

    def _check_punctuation_inside_citation(self) -> List[ErrorInstance]:
        """Flag a period placed right after a citation bracket mid-sentence:
        '[1]. shows' should be '[1] shows.'"""
        errors: List[ErrorInstance] = []
        # Match [N]. followed by a space and then a letter (sentence continues)
        pattern = re.compile(r'(\[\d+\])\.\s+[a-zA-Z]')
        for line_text, line_bbox, page_num in self.line_info:
            for match in pattern.finditer(line_text):
                citation = match.group(1)
                errors.append(ErrorInstance(
                    check_id=31,
                    check_name="Punctuation Inside Citation",
                    description=(
                        f"Period placed after citation '{citation}' mid-sentence; "
                        f"move the period to the end of the sentence"
                    ),
                    page_num=page_num,
                    text=f"{citation}.",
                    bbox=self._calculate_match_bbox(line_text, match, line_bbox),
                    error_type="citation_format",
                ))
        return errors

    def _check_multiple_spaces_between_words(self) -> List[ErrorInstance]:
        """Flag two or more consecutive horizontal spaces between words."""
        errors: List[ErrorInstance] = []
        pattern = re.compile(r'[^\S\n]{2,}')
        for line_text, line_bbox, page_num in self.line_info:
            for match in pattern.finditer(line_text):
                n = len(match.group())
                errors.append(ErrorInstance(
                    check_id=32,
                    check_name="Multiple Spaces Between Words",
                    description=f"Found {n} consecutive spaces — should be a single space",
                    page_num=page_num,
                    text=repr(match.group()),
                    bbox=self._calculate_match_bbox(line_text, match, line_bbox),
                    error_type="spacing_error",
                ))
        return errors

    def _check_space_inside_brackets(self) -> List[ErrorInstance]:
        """Flag spaces immediately after '(' or before ')': '( value )' or '(value )'."""
        errors: List[ErrorInstance] = []
        open_pat  = re.compile(r'\(\s+(?=\S)')   # space after '(' before non-space
        close_pat = re.compile(r'(?<=\S)\s+\)')  # space before ')' after non-space
        for line_text, line_bbox, page_num in self.line_info:
            for match in open_pat.finditer(line_text):
                errors.append(ErrorInstance(
                    check_id=33,
                    check_name="Space Inside Brackets",
                    description="Remove space after opening parenthesis",
                    page_num=page_num,
                    text=match.group() + (line_text[match.end()] if match.end() < len(line_text) else ''),
                    bbox=self._calculate_match_bbox(line_text, match, line_bbox),
                    error_type="spacing_error",
                ))
            for match in close_pat.finditer(line_text):
                errors.append(ErrorInstance(
                    check_id=33,
                    check_name="Space Inside Brackets",
                    description="Remove space before closing parenthesis",
                    page_num=page_num,
                    text=(line_text[match.start() - 1] if match.start() > 0 else '') + match.group(),
                    bbox=self._calculate_match_bbox(line_text, match, line_bbox),
                    error_type="spacing_error",
                ))
        return errors

    # =========================================================================
    # CHECKS #19 & #20 — CAPTION PLACEMENT (PyMuPDF + Camelot)
    # ─────────────────────────────────────────────────────────────────────────
    # Expectations:
    #   • Figure captions must appear BELOW the figure (image).
    #   • Table captions must appear ABOVE the table.
    #
    # Data sources (NO GROBID):
    #   • Figures → PyMuPDF image blocks from page.get_text("dict") (type==1)
    #   • Tables  → Camelot-detected tables (self.extracted_tables) which
    #               reliably identify tabular regions.  Camelot gives us
    #               bboxes in PDF-native (bottom-left-origin) coords + page
    #               height, so we convert to PyMuPDF's top-left origin.
    #   • Captions → PyMuPDF text lines matching STRICT regexes:
    #       Figure: ^(Figure|Fig\.)\s+\d+\.   (period after number mandatory)
    #       Table:  ^Table\s+\d+\.
    #
    # Error conditions (one ErrorInstance per problem):
    #   1. Figure caption ABOVE its image                → misplaced
    #   2. Table caption BELOW its table                 → misplaced
    #   3. Caption with no matching figure/table nearby  → orphan caption
    #   4. Image with no caption nearby                  → uncaptioned figure
    #   5. Camelot table with no caption nearby          → uncaptioned table
    # =========================================================================

    # Strict caption regexes — the trailing period after the number is mandatory.
    _FIG_CAPTION_PREFIX_RE = re.compile(
            r"^\s*(Figure|Fig\.)\s+(\d+)[\.:]",
            re.IGNORECASE
        )

    _TBL_CAPTION_PREFIX_RE = re.compile(
            r"^\s*Table\s+(\d+)[\.:]",
            re.IGNORECASE
        )


    # Proximity window (in PDF points) for "nearest neighbour" decisions.
    _CAPTION_PROX_PT = 800.0

    # Ignore tiny inline images (icons, logos, inline math bitmaps) — they
    # rarely carry captions and pollute orphan-figure detection.
    _MIN_FIGURE_AREA_PT2 = 5000.0  # ≈ 70×70 pt, well below a real figure

    @staticmethod
    def _bbox_y_range(bbox) -> Tuple[float, float]:
        return float(bbox[1]), float(bbox[3])

    @staticmethod
    def _bbox_overlap_y(a, b) -> bool:
        """Return True when two bboxes overlap vertically."""
        return not (a[3] < b[1] or b[3] < a[1])

    def _camelot_tables_for_page(self, page_num_0: int, page_height: float) -> List[Dict]:
        """
        Return Camelot-detected tables on page `page_num_0` (0-indexed) with
        bboxes converted to PyMuPDF top-left origin.

        Camelot bbox is (x1, y1_bottom, x2, y2_top) with origin at bottom-left.
        PyMuPDF bbox is (x0, y0_top, x1, y1_bottom) with origin at top-left.
        """
        out = []
        target_page_1 = page_num_0 + 1
        for t in self.extracted_tables:
            if int(t.get("page") or 0) != target_page_1:
                continue
            cam_bbox = t.get("bbox_pdf")
            if not cam_bbox or len(cam_bbox) != 4:
                continue
            cam_ph = t.get("page_height") or page_height
            x0, y1_bot, x1, y2_top = cam_bbox
            # Convert y: PyMuPDF y0 = page_height - camelot_y2_top
            y0_top    = cam_ph - y2_top
            y1_bottom = cam_ph - y1_bot
            out.append({
                "index": t.get("index"),
                "bbox":  (float(x0), float(y0_top), float(x1), float(y1_bottom)),
            })
        return out

    def _extract_page_layout(self, page) -> Dict:
        """
        Extract per-page image blocks and text lines (with strict caption
        match flags) using PyMuPDF's structured `dict` output.
        """
        raw = page.get_text("dict")
        blocks = raw.get("blocks") or []

        images: List[Dict] = []
        text_lines: List[Dict] = []
        font_sizes: List[float] = []

        for b in blocks:
            btype = b.get("type")
            if btype == 1:
                bbox = tuple(b.get("bbox") or (0, 0, 0, 0))
                area = max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])
                if area < self._MIN_FIGURE_AREA_PT2:
                    continue
                images.append({"bbox": bbox, "area": area})
                continue

            if btype != 0:
                continue

            for ln in b.get("lines") or []:
                spans = ln.get("spans") or []
                if not spans:
                    continue
                text = "".join(s.get("text", "") for s in spans)
                stripped = text.strip()
                if not stripped:
                    continue
                size = float(spans[0].get("size") or 0.0)
                if size > 0:
                    font_sizes.append(size)

                fig_m = self._FIG_CAPTION_PREFIX_RE.match(stripped)
                tbl_m = self._TBL_CAPTION_PREFIX_RE.match(stripped)
                caption_kind = None
                caption_number = None
                if fig_m:
                    caption_kind = "figure"
                    caption_number = int(fig_m.group(2))
                elif tbl_m:
                    caption_kind = "table"
                    caption_number = int(tbl_m.group(1))

                text_lines.append({
                    "text": stripped,
                    "bbox": tuple(ln.get("bbox") or (0, 0, 0, 0)),
                    "size": size,
                    "caption_kind":   caption_kind,
                    "caption_number": caption_number,
                })

        body_font = 0.0
        if font_sizes:
            sorted_sizes = sorted(font_sizes)
            body_font = sorted_sizes[len(sorted_sizes) // 2]

        return {
            "images":     images,
            "text_lines": text_lines,
            "body_font":  body_font,
        }

    def _check_caption_placement(self, doc: "fitz.Document") -> List[ErrorInstance]:
        """Unified figure + table caption-placement check."""
        errors: List[ErrorInstance] = []
        prox = self._CAPTION_PROX_PT

        for page_num in range(len(doc)):
            if page_num < self.start_page_0:
                continue
            page = doc[page_num]
            page_h = float(page.rect.height)
            layout = self._extract_page_layout(page)
            images     = layout["images"]
            text_lines = layout["text_lines"]
            tables     = self._camelot_tables_for_page(page_num, page_h)

            fig_captions = [
                ln for ln in text_lines if ln["caption_kind"] == "figure"
            ]
            tbl_captions = [
                ln for ln in text_lines if ln["caption_kind"] == "table"
            ]

            used_image_ids: Set[int] = set()
            used_table_ids: Set[int] = set()

            # ── 1. Figure captions → nearest image ──────────────────────────
            for cap in fig_captions:
                cap_y0, cap_y1 = self._bbox_y_range(cap["bbox"])

                candidates = []
                for idx, img in enumerate(images):
                    img_y0, img_y1 = self._bbox_y_range(img["bbox"])
                    dy = min(abs(cap_y0 - img_y1), abs(img_y0 - cap_y1))
                    if dy <= prox:
                        candidates.append((dy, idx, img_y0, img_y1))

                if not candidates:
                    errors.append(ErrorInstance(
                        check_id=19,
                        check_name="Figure Caption Placement",
                        description=(
                            f"Figure caption '{cap['text'][:80]}' has no "
                            "associated image on the page."
                        ),
                        page_num=page_num,
                        text=cap["text"][:100],
                        bbox=cap["bbox"],
                        error_type="caption_placement",
                    ))
                    continue

                candidates.sort(key=lambda t: t[0])
                _, img_idx, img_y0, img_y1 = candidates[0]
                used_image_ids.add(img_idx)

                # Caption BELOW the image → correct (cap_y0 >= img_y1).
                if cap_y0 < img_y0:
                    errors.append(ErrorInstance(
                        check_id=19,
                        check_name="Figure Caption Placement",
                        description=(
                            "Figure caption is placed ABOVE its image; "
                            "IEEE requires the caption BELOW the figure."
                        ),
                        page_num=page_num,
                        text=cap["text"][:100],
                        bbox=cap["bbox"],
                        error_type="caption_placement",
                    ))

            # ── 2. Table captions → nearest Camelot table ───────────────────
            for cap in tbl_captions:
                cap_y0, cap_y1 = self._bbox_y_range(cap["bbox"])

                candidates = []
                for t in tables:
                    t_y0, t_y1 = self._bbox_y_range(t["bbox"])
                    dy = min(abs(cap_y0 - t_y1), abs(t_y0 - cap_y1))
                    if dy <= prox:
                        candidates.append((dy, t, t_y0, t_y1))

                if not candidates:
                    errors.append(ErrorInstance(
                        check_id=20,
                        check_name="Table Caption Placement",
                        description=(
                            f"Table caption '{cap['text'][:80]}' has no "
                            "associated table on the page."
                        ),
                        page_num=page_num,
                        text=cap["text"][:100],
                        bbox=cap["bbox"],
                        error_type="caption_placement",
                    ))
                    continue

                candidates.sort(key=lambda c: c[0])
                _, tbl, t_y0, t_y1 = candidates[0]
                used_table_ids.add(int(tbl["index"]))

                # Caption ABOVE the table → correct (cap_y1 <= t_y0).
                # If the caption sits BELOW the table's bottom edge → misplaced.
                if cap_y0 > t_y1:
                    errors.append(ErrorInstance(
                        check_id=20,
                        check_name="Table Caption Placement",
                        description=(
                            "Table caption is placed BELOW its table; "
                            "IEEE requires the caption ABOVE the table."
                        ),
                        page_num=page_num,
                        text=cap["text"][:100],
                        bbox=cap["bbox"],
                        error_type="caption_placement",
                    ))

            # ── 3. Orphan images (no caption claimed them) ──────────────────
            for idx, img in enumerate(images):
                if idx in used_image_ids:
                    continue
                img_y0, img_y1 = self._bbox_y_range(img["bbox"])
                has_nearby_caption = any(
                    min(abs(self._bbox_y_range(c["bbox"])[0] - img_y1),
                        abs(img_y0 - self._bbox_y_range(c["bbox"])[1])) <= prox
                    for c in fig_captions
                )
                if has_nearby_caption:
                    continue
                errors.append(ErrorInstance(
                    check_id=19,
                    check_name="Missing Figure Caption",
                    description=(
                        "A figure/image has no caption nearby. Every figure "
                        "must have a caption in the form 'Fig. N.' or "
                        "'Figure N.' placed BELOW the image."
                    ),
                    page_num=page_num,
                    text="[Uncaptioned figure on this page]",
                    bbox=img["bbox"],
                    error_type="caption_placement",
                ))

            # ── 4. Orphan Camelot tables (no caption claimed them) ─────────
            for tbl in tables:
                if int(tbl["index"]) in used_table_ids:
                    continue
                t_y0, t_y1 = self._bbox_y_range(tbl["bbox"])
                has_nearby_caption = any(
                    min(abs(self._bbox_y_range(c["bbox"])[0] - t_y1),
                        abs(t_y0 - self._bbox_y_range(c["bbox"])[1])) <= prox
                    for c in tbl_captions
                )
                if has_nearby_caption:
                    continue
                errors.append(ErrorInstance(
                    check_id=20,
                    check_name="Missing Table Caption",
                    description=(
                        "A table has no caption nearby. Every table must "
                        "have a caption in the form 'Table N.' placed "
                        "ABOVE the table."
                    ),
                    page_num=page_num,
                    text="[Uncaptioned table on this page]",
                    bbox=tbl["bbox"],
                    error_type="caption_placement",
                ))

        return errors

    # =========================================================================
    # CHECK #27 — REQUIRED SECTIONS (format-driven, called externally)
    # ─────────────────────────────────────────────────────────────────────────
    # The single streamlined section-existence check. Reads
    # self.section_detection (populated by _detect_all_sections) and emits one
    # ErrorInstance per required-but-missing section.
    # =========================================================================

    def _check_required_sections(self, required: List[str]) -> List[ErrorInstance]:
        if not required:
            return []

        # Safety: detect sections on demand if something skipped the main call.
        if not self.section_detection:
            self._detect_all_sections()

        errors = []
        for section in required:
            info = self.section_detection.get(section) or {
                "found": False, "matched_heading": None, "page": None,
            }
            if info["found"]:
                continue
            errors.append(ErrorInstance(
                check_id=27,
                check_name=f"Required Section Missing: {section}",
                description=(
                    f"The required section '{section}' was not found in the document. "
                    "Ensure this section is present and clearly labelled."
                ),
                page_num=0,
                text=section,  # Plain section name — used by the frontend UI.
                bbox=(0.0, 0.0, 200.0, 20.0),
                error_type="missing_required_section",
            ))
        return errors

    # =========================================================================
    # HELPERS  (unchanged)
    # =========================================================================

    def _is_likely_equation(self, text: str) -> bool:
        score = 0
        line = text.strip()
        if len(line) < 3:
            return False
        if re.search(r"[=+\*^×÷≤≥≈≠∑∫∂∇√∏∆λμπσΩαβγδεθ]", line):
            score += 2
        if re.search(r"\b[a-zA-Z]\b", line) and re.search(r"[=+\-*/]", line):
            score += 2
        if re.search(r"\(\d+\)\s*$", line):
            score += 5
        if re.search(r"[_^]\{?\w+\}?|\w+_\d+|\w+\^\d+", line):
            score += 2
        if re.search(r"[αβγδεζηθικλμνξοπρστυφχψωΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ]", line):
            score += 1
        if re.search(r"\([^)]+\).*[=+\-*/]|[=+\-*/].*\([^)]+\)", line):
            score += 1
        common_words = len(re.findall(
            r"\b(the|and|is|of|in|to|for|with|this|that|are|was|were|be|been|"
            r"being|have|has|had|do|does|did|will|would|should|could|can|may|might)\b",
            line.lower(),
        ))
        if common_words > 2:
            score -= 3
        if re.match(r"^(The|This|That|These|Those|In|For|However|Therefore|Thus|Hence)\b", line):
            score -= 2
        if len(line) > 150:
            score -= 1
        if re.match(r"^\(\d+\)\s+[A-Z][a-z]+", line):
            score -= 4
        words = re.findall(r"\b[a-zA-Z]{3,}\b", line)
        math_symbols = re.findall(r"[=+\-*/^×÷≤≥≈≠∑∫∂∇√]", line)
        if len(words) > 5 and len(math_symbols) < 2:
            score -= 2
        return score >= 4

    def _calculate_match_bbox(
        self,
        full_line: str,
        match: re.Match,
        line_bbox: Tuple[float, float, float, float],
        padding: int = 2,
    ) -> Tuple[float, float, float, float]:
        text_len = len(full_line)
        if text_len == 0:
            return line_bbox
        x0, y0, x1, y1 = line_bbox
        char_w = (x1 - x0) / text_len
        mx0 = x0 + match.start() * char_w
        mx1 = x0 + match.end() * char_w
        return (
            max(x0, mx0 - padding),
            y0 - padding,
            min(x1, mx1 + padding),
            y1 + padding,
        )

    # =========================================================================
    # PDF ANNOTATION  — PyMuPDF retained (GROBID cannot write PDFs)
    # =========================================================================

    def annotate_pdf(
        self,
        doc: fitz.Document,
        errors: List[ErrorInstance],
        output_path: str,
    ):
        """Write highlight annotations + visible margin notes for every ErrorInstance."""
        color_map = {
            "non_roman_heading":             (0.90, 0.90, 0.50),
            "non_ieee_citation":             (1.00, 0.75, 0.75),
            "non_ieee_reference_format":     (0.85, 0.95, 1.00),
            "invalid_figure_label":          (0.95, 0.85, 1.00),
            "invalid_table_numbering":       (0.80, 0.95, 0.85),
            "equation_numbering":            (1.00, 0.90, 0.70),
            "figure_numbering_sequence":     (0.95, 0.80, 0.95),
            "table_numbering_sequence":      (0.80, 0.95, 0.90),
            "reference_numbering_sequence":  (0.85, 0.85, 1.00),
            "broken_url":                    (1.00, 0.85, 0.85),
            "broken_doi":                    (1.00, 0.85, 0.85),
            "metadata_incomplete":           (1.00, 0.75, 0.55),
            "missing_required_section":      (1.00, 0.65, 0.65),
        }

        # One-line labels used in the visible margin note
        short_labels: Dict[str, str] = {
            "non_roman_heading":             "Non-Roman heading",
            "non_ieee_citation":             "Citation format",
            "non_ieee_reference_format":     "Reference format",
            "invalid_figure_label":          "Invalid figure label",
            "invalid_table_numbering":       "Invalid table label",
            "equation_numbering":            "Equation numbering",
            "figure_numbering_sequence":     "Figure seq. error",
            "table_numbering_sequence":      "Table seq. error",
            "reference_numbering_sequence":  "Reference seq. error",
            "broken_url":                    "Broken URL",
            "broken_doi":                    "Broken DOI",
            "metadata_incomplete":           "Missing metadata",
            "missing_required_section":      "Missing section",
            "repeated_word":                 "Repeated word",
            "writing_style":                 "First-person pronoun",
            "punctuation_spacing":           "Punctuation spacing",
            "punctuation_error":             "Punctuation error",
            "citation_format":               "Citation format",
            "spacing_error":                 "Spacing error",
            "caption_placement":             "Caption placement",
        }

        # Margin note layout constants (points)
        NOTE_W      = 110
        NOTE_H      = 32
        NOTE_GAP    = 4       # min vertical gap between stacked notes on same side
        NOTE_FONT   = 6.5
        MARGIN_PAD  = 4       # distance from page edge

        # Track occupied y-ranges per (page_num, side) to avoid overlaps
        # side: "left" or "right"
        slots: Dict[Tuple[int, str], List[float]] = {}

        for error in errors:
            page = doc[error.page_num]
            color = color_map.get(error.error_type, (1.00, 1.00, 0.60))

            # ── Highlight ────────────────────────────────────────────────────
            hl = page.add_highlight_annot(error.bbox)
            hl.set_colors(stroke=color)
            hl.set_opacity(0.5)
            hl.info["title"]   = f"Check #{error.check_id}: {error.check_name}"
            hl.info["content"] = f"{error.description}\n\nFound: '{error.text}'"
            hl.update()

            # ── Margin note ──────────────────────────────────────────────────
            pw = page.rect.width
            ph = page.rect.height

            # Pick the margin side opposite to where the error text sits
            err_cx = (error.bbox[0] + error.bbox[2]) / 2
            side   = "right" if err_cx < pw / 2 else "left"

            if side == "right":
                nx0 = pw - NOTE_W - MARGIN_PAD
                nx1 = pw - MARGIN_PAD
            else:
                nx0 = MARGIN_PAD
                nx1 = MARGIN_PAD + NOTE_W

            # Ideal y: vertically centered on the highlighted text
            err_cy = (error.bbox[1] + error.bbox[3]) / 2
            ny0    = max(MARGIN_PAD, err_cy - NOTE_H / 2)
            ny1    = ny0 + NOTE_H

            # Shift down to avoid overlapping earlier notes on the same side
            key = (error.page_num, side)
            occupied = slots.get(key, [])
            for prev_y1 in occupied:
                if ny0 < prev_y1 + NOTE_GAP:
                    ny0 = prev_y1 + NOTE_GAP
                    ny1 = ny0 + NOTE_H

            # Clamp to page bottom
            if ny1 > ph - MARGIN_PAD:
                ny1 = ph - MARGIN_PAD
                ny0 = max(MARGIN_PAD, ny1 - NOTE_H)

            slots.setdefault(key, []).append(ny1)

            label      = short_labels.get(error.error_type, error.check_name)
            snippet    = error.text[:35] + ("…" if len(error.text) > 35 else "")
            note_text  = f"{label}:\n{snippet}"

            ft = page.add_freetext_annot(
                fitz.Rect(nx0, ny0, nx1, ny1),
                note_text,
                fontsize=NOTE_FONT,
                fontname="helv",
                text_color=(0.0, 0.0, 0.0),
                fill_color=color,
                align=fitz.TEXT_ALIGN_LEFT,
            )
            ft.set_border(width=0.6)
            ft.set_opacity(0.88)
            ft.update()

        doc.save(output_path, garbage=4, deflate=True)
        doc.close()


# =============================================================================
# ENTRY POINT
# =============================================================================

def process_pdf(
    input_path: str,
    output_path: str,
    required_sections: Optional[List[str]] = None,
    enabled_check_types: Optional[Set[str]] = None,
    start_page: int = 1,
    progress_callback=None,
) -> Tuple[List[ErrorInstance], str, Dict, Dict, Dict]:
    """
    Full pipeline: open PDF → detect errors → annotate → save.

    Args:
        input_path          – path to the source PDF
        output_path         – path where the annotated PDF is written
        required_sections   – sections that must exist (format-driven)
        enabled_check_types – set of error_type strings to keep; None = keep all
        start_page          – 1-indexed page to begin processing from (skips earlier pages)
        progress_callback   – optional callable(check_key: str, check_name: str) fired after
                              each logical check group completes, for live progress reporting

    Returns:
        errors             – list of ErrorInstance objects (filtered)
        output_path        – path to the annotated PDF
        statistics         – document statistics dict
        extracted_data     – raw extracted text and line data
        reference_analysis – reference quality analysis from external API
    """
    detector = PDFErrorDetector(start_page=start_page)
    errors, doc, statistics = detector.detect_errors(
        input_path, required_sections, progress_callback=progress_callback
    )

    # Apply format whitelist: keep only errors whose type is enabled
    if enabled_check_types is not None:
        errors = [e for e in errors if e.error_type in enabled_check_types]

    detector.annotate_pdf(doc, errors, output_path)
    extracted_data = detector.export_extracted_data()
    return errors, output_path, statistics, extracted_data, detector.reference_analysis