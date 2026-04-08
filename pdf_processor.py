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
    ("abstract_exists", {
        "name": "Abstract Section Exists",
        "description": "Paper contains an Abstract section",
        "category": "Structure",
        "error_types": ["missing_abstract"],
        "default": True,
    }),
    ("abstract_word_count", {
        "name": "Abstract Word Count (150–250 words)",
        "description": "Abstract must be between 150 and 250 words",
        "category": "Structure",
        "error_types": ["abstract_word_count"],
        "default": True,
    }),
    ("index_terms", {
        "name": "Index Terms / Keywords",
        "description": "Paper contains an Index Terms or Keywords section",
        "category": "Structure",
        "error_types": ["missing_index_terms"],
        "default": True,
    }),
    ("references_section", {
        "name": "References Section Exists",
        "description": "Paper contains a References section",
        "category": "Structure",
        "error_types": ["missing_references"],
        "default": True,
    }),
    ("roman_numeral_headings", {
        "name": "Roman Numeral Section Headings",
        "description": "Section headings use Roman numerals (e.g. I. INTRODUCTION)",
        "category": "Structure",
        "error_types": ["non_roman_heading"],
        "default": True,
    }),
    ("introduction_section", {
        "name": "Introduction Section (I. INTRODUCTION)",
        "description": "Paper has a correctly formatted Introduction section",
        "category": "Structure",
        "error_types": ["missing_introduction"],
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
        "default": False,
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
    "Acknowledgments":["acknowledgment", "acknowledgement", "acknowledgments"],
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

    GROBID_URL = "https://ashjin-grobid-local-2.hf.space/"

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
        # Used by _check_roman_numeral_headings() and _check_introduction_exists().

        self._grobid_has_abstract: bool = False
        self._grobid_abstract_text: str = ""
        self._grobid_has_keywords: bool = False
        # Used by _check_abstract_exists() / _check_index_terms_exists() /
        # _check_abstract_word_count().

        self._grobid_figure_entries: List[Dict] = []
        # Full figure list with page + coords, used by _check_figure_caption_placement().
        # (grobid_figures is the same list — kept for backward compat with export_extracted_data.)

        self._grobid_table_entries: List[Dict] = []
        # Full table list with page + coords, used by _check_table_caption_placement().

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
        Use the already-parsed TEI root (stored during _extract_with_grobid)
        to rebuild line_info from <s>/<w> tokens with GROBID coords.

        This is called AFTER _extract_with_grobid() has run and stored
        self._tei_root.  If the attribute is absent (GROBID call failed),
        raises AttributeError so the caller can fall back.
        """
        root = self._tei_root  # set by _extract_with_grobid(); AttributeError if absent
        ns = {"tei": "http://www.tei-c.org/ns/1.0"}

        # Collect per-page plain text for page_texts
        num_pages = len(doc)
        page_text_lists: List[List[str]] = [[] for _ in range(num_pages)]

        current_offset = 0

        # Walk every <s> (sentence) element — they span multiple <w> tokens.
        # We reconstruct logical "lines" by grouping tokens whose GROBID
        # page+y coords are within a small tolerance of each other.
        sentences = root.findall(".//tei:s", ns)

        if not sentences:
            print("[GROBID] No <s> tags — falling back to paragraphs")
            
            paragraphs = root.findall(".//tei:p", ns)
            sentences = []

            for p in paragraphs:
                text = "".join(p.itertext()).strip()
                if text:
                    sentences.extend(
                        re.split(r'(?<=[.!?])\s+', text)
                    )

        for sent in sentence_elements:
            # Each sentence becomes one logical "line" in line_info.
            words = []
            coords_list = []

            for token in sent.iter():
                text = (token.text or "").strip()
                if not text:
                    continue
                coords_str = token.get("coords", "")
                if coords_str:
                    coords_list.append(coords_str)
                words.append(text)

            if not words:
                continue

            line_text = " ".join(words)

            # Parse GROBID coords: "page,x0,y0,x1,y1" — take the first token's
            # coords for the representative bbox and page number.
            page_num, bbox = self._parse_grobid_coords(coords_list, fallback_page=0)
            page_num = min(page_num, num_pages - 1)  # clamp

            if page_num < self.start_page_0:
                continue

            self.line_info.append((line_text, bbox, page_num))
            self.line_offsets.append(current_offset)
            self.full_text += line_text + "\n"
            current_offset += len(line_text) + 1

            page_text_lists[page_num].append(line_text)

        self.page_texts = ["\n".join(lines) for lines in page_text_lists]
        print(f"[TEXT EXTRACT] GROBID: {len(self.line_info)} logical lines across "
              f"{num_pages} pages.")

    def _parse_grobid_coords(
        self,
        coords_list: List[str],
        fallback_page: int = 0,
    ) -> Tuple[int, Tuple[float, float, float, float]]:
        """
        Parse a list of GROBID coords strings and return
        (page_num, union_bbox) covering all tokens.

        GROBID emits coordinates in two forms:
          • Simple:  "page,x0,y0,x1,y1"
          • Multi:   "page,x0,y0,x1,y1;page,x0,y0,x1,y1;…"
        Both are handled.  Page numbers are 1-indexed in GROBID;
        we convert to 0-indexed.
        """
        if not coords_list:
            return fallback_page, (0.0, 0.0, 100.0, 14.0)

        pages = []
        x0s, y0s, x1s, y1s = [], [], [], []

        for raw in coords_list:
            # Split on ';' to handle multi-fragment coords strings
            fragments = raw.split(";") if ";" in raw else [raw]
            for frag in fragments:
                parts = frag.strip().split(",")
                if len(parts) < 5:
                    continue
                try:
                    pages.append(int(parts[0]) - 1)
                    x0s.append(float(parts[1]))
                    y0s.append(float(parts[2]))
                    x1s.append(float(parts[3]))
                    y1s.append(float(parts[4]))
                except ValueError:
                    continue

        if not x0s:
            return fallback_page, (0.0, 0.0, 100.0, 14.0)

        page_num = pages[0]
        bbox = (min(x0s), min(y0s), max(x1s), max(y1s))
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
                table_data = {
                    "index": idx,
                    "page": table.page,
                    "dataframe": table.df,
                    "headers": table.df.iloc[0].tolist() if len(table.df) > 0 else [],
                    "accuracy": table.accuracy,
                    "whitespace": table.whitespace,
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
                    timeout=60,
                    data={
                        "teiCoordinates": "true",
                        "segmentSentences": "true",
                        "consolidateHeader": "1",
                        "consolidateCitations": "1",
                    },
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
            # _check_table_caption_placement() uses its page-height fallback.
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

    def _extract_citations_grobid(self, pdf_path: str) -> List[Dict]:
        """
        Use GROBID /api/processReferences to extract the reference list.
        Returns a list of raw reference dicts.  Unchanged from original.
        """
        citations = []
        try:
            print("[GROBID CITATIONS] Extracting references...")
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
                parts = []

                for author in ref.findall(".//tei:author", ns):
                    surname = author.findtext(".//tei:surname", default="", namespaces=ns)
                    forename = author.findtext(".//tei:forename", default="", namespaces=ns)
                    if surname:
                        parts.append(f"{forename} {surname}".strip())

                title = ref.findtext(".//tei:title[@level='a']", default="", namespaces=ns)
                if not title:
                    title = ref.findtext(".//tei:title", default="", namespaces=ns)
                if title:
                    parts.append(title)

                journal = ref.findtext(".//tei:title[@level='j']", default="", namespaces=ns)
                if not journal:
                    journal = ref.findtext(".//tei:title[@level='m']", default="", namespaces=ns)
                if journal:
                    parts.append(journal)

                date = ref.findtext(".//tei:date[@type='published']", default="", namespaces=ns)
                if not date:
                    date_el = ref.find(".//tei:date", ns)
                    date = date_el.get("when", "") if date_el is not None else ""
                if date:
                    parts.append(date[:4])

                vol  = ref.findtext(".//tei:biblScope[@unit='volume']", default="", namespaces=ns)
                iss  = ref.findtext(".//tei:biblScope[@unit='issue']",  default="", namespaces=ns)
                page = ref.findtext(".//tei:biblScope[@unit='page']",   default="", namespaces=ns)
                if vol:  parts.append(f"vol. {vol}")
                if iss:  parts.append(f"no. {iss}")
                if page: parts.append(f"pp. {page}")

                doi = ref.findtext(".//tei:idno[@type='DOI']", default="", namespaces=ns)
                if doi:
                    parts.append(f"doi:{doi}")

                raw_text = " ".join(p for p in parts if p).strip()
                if raw_text:
                    citations.append({"raw_text": raw_text})

            print(f"[GROBID CITATIONS] Extracted {len(citations)} references")

        except requests.exceptions.Timeout:
            print("[GROBID CITATIONS] Timeout — GROBID service took too long")
        except requests.exceptions.ConnectionError:
            print("[GROBID CITATIONS] Connection error — GROBID not available")
        except Exception as e:
            print(f"[GROBID CITATIONS] Error: {e}")
            import traceback; traceback.print_exc()

        return citations

    @staticmethod
    def analyze_references(citations: List[Dict]) -> Dict:
        """Send citations to the configured reference API endpoint."""
        if not citations:
            return {}

        REFERENCE_API = os.environ.get("REFERENCE_API_URL") or os.environ.get("REFERENCE_API")
        if not REFERENCE_API:
            return {"error": "REFERENCE_API_URL is not configured"}

        payload = {
            "entries": citations,
            "dry_run": False,
            "deep_doi": False,
            "crossref_email": None,
        }

        try:
            print(f"[REF API] Sending {len(citations)} references for analysis...")
            resp = requests.post(
                REFERENCE_API,
                json=payload,
                timeout=60,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            result = resp.json()
            print(f"[REF API] Response: {result.get('summary', {})}")
            return result
        except requests.exceptions.Timeout:
            print("[REF API] Timeout")
            return {"error": "Reference API timed out"}
        except requests.exceptions.ConnectionError as e:
            print(f"[REF API] Connection error: {e}")
            return {"error": "Could not connect to reference API"}
        except Exception as e:
            print(f"[REF API] Error: {e}")
            return {"error": str(e)}

    # =========================================================================
    # STATISTICS  — figure count now from GROBID
    # =========================================================================

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
        }

    # =========================================================================
    # PUBLIC API
    # =========================================================================

    def detect_errors(
        self,
        pdf_path: str,
        required_sections: Optional[List[str]] = None,
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
        self.pipeline_status["pix2text"] = {
            "enabled": False,
            "success": False,
            "message": "Pix2Text equation extraction disabled",
            "count": 0,
        }

        citations = self._extract_citations_grobid(pdf_path)
        self.reference_analysis = self.analyze_references(citations)
        self.raw_citations = citations

        try:
            self._build_merged_blocks()
            self.pipeline_status["merge"] = {"success": True, "message": "Merge complete"}
        except Exception as exc:
            print(f"[MERGE] Error: {exc}")
            self.merged_blocks = []
            self.merge_summary = {}
            self.pipeline_status["merge"] = {"success": False, "message": f"Merge failed: {exc}"}

        statistics = self._collect_statistics(doc)
        errors = self._run_document_checks(doc)

        # Format-driven required-sections check (optional, caller-supplied list)
        if required_sections:
            errors.extend(self._check_required_sections(required_sections))

        # Filter out errors from pages before start_page
        if self.start_page_0 > 0:
            errors = [e for e in errors if e.page_num >= self.start_page_0]

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

    def _run_document_checks(self, doc: fitz.Document) -> List[ErrorInstance]:
        """Run all compliance and formatting checks."""
        errors = []

        # Metadata Completeness (25) — GROBID header model
        errors.extend(self._check_metadata_completeness())

        # Structure & Content Checks (1–5)
        errors.extend(self._check_abstract_exists())
        # errors.extend(self._check_abstract_word_count())
        errors.extend(self._check_index_terms_exists())
        errors.extend(self._check_references_section_exists())
        errors.extend(self._check_roman_numeral_headings())
        errors.extend(self._check_introduction_exists())

        # Format Checks (6–8): label format
        errors.extend(self._check_figure_numbering())
        errors.extend(self._check_table_numbering())
        errors.extend(self._check_equation_numbering())

        # Sequential Numbering Checks (21–23)
        errors.extend(self._check_figure_sequential_numbering())
        errors.extend(self._check_table_sequential_numbering())
        errors.extend(self._check_reference_sequential_numbering())

        # Figure/Table Caption Placement (19–20)
        errors.extend(self._check_figure_caption_placement())
        errors.extend(self._check_table_caption_placement())

        # URL & DOI Validity (24)
        errors.extend(self._check_url_doi_validity())

        # Typography & Formatting Checks (12, 15–17)
        errors.extend(self._check_repeated_words())
        errors.extend(self._check_et_al_formatting())
        errors.extend(self._check_first_person_pronouns())
        errors.extend(self._check_references_numbered())

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
    # CHECK #1 — ABSTRACT EXISTS
    # =========================================================================

    def _check_abstract_exists(self) -> List[ErrorInstance]:
        """
        Verify the paper contains an Abstract.

        Primary:  GROBID's <abstract> element (structural, avoids false positives
                  from the word "abstract" appearing inside body sentences).
        Fallback: regex on full_text (when GROBID was unavailable).
        """
        # Primary: GROBID structural signal
        if self._grobid_has_abstract:
            return []

        # Fallback: regex
        if re.search(r"\bAbstract\b", self.full_text, re.IGNORECASE):
            return []

        first_text, first_bbox, first_page = (
            self.line_info[0] if self.line_info else ("", (0, 0, 200, 20), 0)
        )
        return [ErrorInstance(
            check_id=1,
            check_name="Abstract Missing",
            description="No Abstract section found. IEEE papers must include an Abstract at the beginning.",
            page_num=first_page,
            text="[Abstract section not found]",
            bbox=first_bbox,
            error_type="missing_abstract",
        )]

    # =========================================================================
    # CHECK #26 — ABSTRACT WORD COUNT (150–250 words)
    # =========================================================================

    ABSTRACT_MIN_WORDS = 150
    ABSTRACT_MAX_WORDS = 250

    _HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
    _ABSTRACT_HEADING_RE = re.compile(
        r"^#{1,6}\s+Abstract\s*$", re.MULTILINE | re.IGNORECASE
    )

    def _extract_abstract_from_markdown(self) -> str:
        """
        Extract the abstract body from the pymupdf4llm markdown text.

        Finds the first heading matching "Abstract" (any level) and returns
        everything between it and the next markdown heading.  Returns an
        empty string when the markdown is unavailable or contains no
        recognisable Abstract heading.
        """
        if not self.markdown_text:
            return ""

        m = self._ABSTRACT_HEADING_RE.search(self.markdown_text)
        if not m:
            return ""

        body_start = m.end()
        next_heading = self._HEADING_RE.search(self.markdown_text, body_start)
        body_end = next_heading.start() if next_heading else len(self.markdown_text)

        return self.markdown_text[body_start:body_end].strip()

    # def _check_abstract_word_count(self) -> List[ErrorInstance]:
    #     """
    #     Verify the abstract contains between 150 and 250 words.

    #     Uses the pymupdf4llm markdown extraction to isolate the abstract:
    #     everything after the "Abstract" heading up to the next heading.
    #     Falls back to GROBID abstract text when markdown is unavailable.
    #     """
    #     abstract_text = self._extract_abstract_from_markdown()
    #     if not abstract_text:
    #         abstract_text = self._grobid_abstract_text
    #     if not abstract_text:
    #         return []

    #     words = abstract_text.split()
    #     count = len(words)

    #     if self.ABSTRACT_MIN_WORDS <= count <= self.ABSTRACT_MAX_WORDS:
    #         return []

    #     anchor_text, anchor_bbox, anchor_page = (
    #         self.line_info[0] if self.line_info else ("", (0, 0, 200, 20), 0)
    #     )

    #     if count < self.ABSTRACT_MIN_WORDS:
    #         description = (
    #             f"Abstract is too short: {count} word{'s' if count != 1 else ''}. "
    #             f"IEEE abstracts should be between {self.ABSTRACT_MIN_WORDS} and "
    #             f"{self.ABSTRACT_MAX_WORDS} words."
    #         )
    #     else:
    #         description = (
    #             f"Abstract is too long: {count} words. "
    #             f"IEEE abstracts should be between {self.ABSTRACT_MIN_WORDS} and "
    #             f"{self.ABSTRACT_MAX_WORDS} words."
    #         )

    #     return [ErrorInstance(
    #         check_id=26,
    #         check_name="Abstract Word Count Out of Range",
    #         description=description,
    #         page_num=anchor_page,
    #         text=f"[Abstract: {count} words — expected {self.ABSTRACT_MIN_WORDS}–{self.ABSTRACT_MAX_WORDS}]",
    #         bbox=anchor_bbox,
    #         error_type="abstract_word_count",
    #     )]

    # =========================================================================
    # CHECK #2 — INDEX TERMS EXISTS
    # =========================================================================

    def _check_index_terms_exists(self) -> List[ErrorInstance]:
        """
        Verify the paper contains Index Terms / Keywords.

        Primary:  GROBID's <textClass><keywords> element.
        Fallback: regex on full_text.
        """
        if self._grobid_has_keywords:
            return []

        if re.search(r"Index\s+Terms", self.full_text, re.IGNORECASE):
            return []

        first_text, first_bbox, first_page = (
            self.line_info[0] if self.line_info else ("", (0, 0, 200, 20), 0)
        )
        return [ErrorInstance(
            check_id=2,
            check_name="Index Terms Missing",
            description="No Index Terms section found. IEEE papers require Index Terms following the Abstract.",
            page_num=first_page,
            text="[Index Terms section not found]",
            bbox=first_bbox,
            error_type="missing_index_terms",
        )]

    # =========================================================================
    # CHECK #3 — REFERENCES SECTION EXISTS
    # =========================================================================

    def _check_references_section_exists(self) -> List[ErrorInstance]:
        """
        Verify the paper contains a References section.

        Primary:  Non-empty raw_citations list from GROBID — if GROBID extracted
                  any references, a reference section must exist.
        Secondary: A heading in _grobid_section_heads named "references".
        Fallback:  regex on full_text.
        """
        if self.raw_citations:
            return []

        if any(
            re.search(r"\breferences?\b", h["text"], re.IGNORECASE)
            for h in self._grobid_section_heads
        ):
            return []

        if re.search(r"\bReferences\b", self.full_text, re.IGNORECASE):
            return []

        last_text, last_bbox, last_page = (
            self.line_info[-1] if self.line_info else ("", (0, 0, 200, 20), 0)
        )
        return [ErrorInstance(
            check_id=3,
            check_name="References Section Missing",
            description="No References section found. IEEE papers must include a References section at the end.",
            page_num=last_page,
            text="[References section not found]",
            bbox=last_bbox,
            error_type="missing_references",
        )]

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
    # CHECK #5 — INTRODUCTION SECTION EXISTS
    # =========================================================================

    def _check_introduction_exists(self) -> List[ErrorInstance]:
        """
        Verify the paper contains 'I. INTRODUCTION'.

        Primary:  Check _grobid_section_heads for a heading whose text matches
                  the expected IEEE format.  Also detect mis-formatted
                  "Introduction" headings among GROBID heads.
        Fallback: Original regex on full_text.
        """
        ieee_intro_re = re.compile(r"\bI\.\s+INTRODUCTION\b")
        generic_intro_re = re.compile(r"\bIntroduction\b", re.IGNORECASE)

        if self._grobid_section_heads:
            # ── GROBID path ──────────────────────────────────────────────────
            # Check for correctly-formatted heading
            if any(ieee_intro_re.search(h["text"]) for h in self._grobid_section_heads):
                return []

            # Check for mis-formatted introduction headings
            bad_intro_heads = [
                h for h in self._grobid_section_heads
                if generic_intro_re.search(h["text"]) and not ieee_intro_re.search(h["text"])
            ]

            if bad_intro_heads:
                errors = []
                for head in bad_intro_heads:
                    errors.append(ErrorInstance(
                        check_id=5,
                        check_name="Introduction Section Misformatted",
                        description=(
                            f"Heading '{head['text']}' found but not in IEEE format. "
                            "It should be labelled 'I. INTRODUCTION' — Roman numeral, fully uppercase."
                        ),
                        page_num=head["page"],
                        text=head["text"],
                        bbox=head["bbox"],
                        error_type="missing_introduction",
                    ))
                return errors

            # No introduction heading at all
            first_text, first_bbox, first_page = (
                self.line_info[0] if self.line_info else ("", (0, 0, 200, 20), 0)
            )
            return [ErrorInstance(
                check_id=5,
                check_name="Introduction Section Missing",
                description="No 'I. INTRODUCTION' section found. IEEE papers require an introduction labelled 'I. INTRODUCTION'.",
                page_num=first_page,
                text="[I. INTRODUCTION not found]",
                bbox=first_bbox,
                error_type="missing_introduction",
            )]

        else:
            # ── Fallback: original regex logic ───────────────────────────────
            if re.search(r"\bI\.\s+INTRODUCTION\b", self.full_text):
                return []

            has_generic = bool(re.search(r"\bIntroduction\b", self.full_text, re.IGNORECASE))
            if has_generic:
                return self._find_all_occurrences(
                    pattern=re.compile(r"\bIntroduction\b", re.IGNORECASE),
                    check_id=5,
                    check_name="Introduction Section Misformatted",
                    error_type="missing_introduction",
                    description_fn=lambda m, line: (
                        "'Introduction' found but not in IEEE format. "
                        "It should be labelled 'I. INTRODUCTION' — Roman numeral, fully uppercase."
                    ),
                )

            first_text, first_bbox, first_page = (
                self.line_info[0] if self.line_info else ("", (0, 0, 200, 20), 0)
            )
            return [ErrorInstance(
                check_id=5,
                check_name="Introduction Section Missing",
                description="No 'I. INTRODUCTION' section found. IEEE papers require an introduction labelled 'I. INTRODUCTION'.",
                page_num=first_page,
                text="[I. INTRODUCTION not found]",
                bbox=first_bbox,
                error_type="missing_introduction",
            )]

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

    def _check_first_person_pronouns(self) -> List[ErrorInstance]:
        errors = []
        pattern = re.compile(r"\b(I|we|our|my|us|We|Our|My|Us)\b")
        for line_text, line_bbox, page_num in self.line_info:
            if "acknowledgment" in line_text.lower() or "acknowledge" in line_text.lower():
                continue
            for match in pattern.finditer(line_text):
                word = match.group()
                idx = match.start()
                if idx > 0 and line_text[idx - 1].isupper():
                    continue
                if match.end() < len(line_text) and line_text[match.end()].isupper():
                    continue
                errors.append(ErrorInstance(
                    check_id=16,
                    check_name="First-Person Pronoun",
                    description=f"IEEE-style papers prefer impersonal tone over first-person pronouns like '{word}'",
                    page_num=page_num,
                    text=word,
                    bbox=self._calculate_match_bbox(line_text, match, line_bbox),
                    error_type="writing_style",
                ))
        return errors

    # =========================================================================
    # CHECK #19 — FIGURE CAPTION PLACEMENT
    # =========================================================================

    def _check_figure_caption_placement(self) -> List[ErrorInstance]:
        """
        Figure captions must be placed BELOW the figure.

        Primary:  Use GROBID-detected figure bounding boxes (_grobid_figure_entries).
                  GROBID provides the actual page-coordinate bbox of each figure
                  (the image area) from the coords attribute.  We then look for a
                  caption line whose y0 is ABOVE the figure's y0 — that's a
                  misplaced caption.

        Fallback: Original page-height threshold heuristic (when GROBID returned
                  no figure data or coords were absent).
        """
        errors = []

        if self._grobid_figure_entries and any(
            e.get("xml_coords") for e in self._grobid_figure_entries
        ):
            # ── GROBID path ──────────────────────────────────────────────────
            fig_pattern = re.compile(
                r"(Fig\.|Figure)\s+(\d+)[:\.]?\s+([^\n]{10,200})", re.IGNORECASE
            )
            for fig_entry in self._grobid_figure_entries:
                fig_page = fig_entry["page"]
                _, fig_y0, _, fig_y1 = fig_entry["bbox"]  # GROBID PDF coords

                # Find caption lines on the same page
                for line_text, line_bbox, page_num in self.line_info:
                    if page_num != fig_page:
                        continue
                    match = fig_pattern.search(line_text)
                    if not match:
                        continue

                    caption_y0 = line_bbox[1]
                    # Caption is above the figure's top edge → misplaced
                    if caption_y0 < fig_y0:
                        errors.append(ErrorInstance(
                            check_id=19,
                            check_name="Figure Caption Placement",
                            description="Figure captions should be placed BELOW the figure, not above",
                            page_num=page_num,
                            text=match.group(0)[:100],
                            bbox=line_bbox,
                            error_type="caption_placement",
                        ))
        else:
            # ── Fallback: page-height heuristic ──────────────────────────────
            print("Fallback: page-height heuristic for figure caption placement")
            fig_pattern = re.compile(
                r"(Fig\.|Figure)\s+(\d+)[:\.]?\s+([^\n]{10,200})", re.IGNORECASE
            )
            for line_text, line_bbox, page_num in self.line_info:
                match = fig_pattern.search(line_text)
                if match and line_bbox[1] < 842 / 3:
                    errors.append(ErrorInstance(
                        check_id=19,
                        check_name="Figure Caption Placement",
                        description="Figure captions should be placed BELOW the figure, not above",
                        page_num=page_num,
                        text=match.group(0)[:100],
                        bbox=line_bbox,
                        error_type="caption_placement",
                    ))

        return errors

    # =========================================================================
    # CHECK #20 — TABLE CAPTION PLACEMENT
    # =========================================================================

    def _check_table_caption_placement(self) -> List[ErrorInstance]:
        """
        Table captions must be placed ABOVE the table.

        Primary:  Use GROBID-detected table bounding boxes (_grobid_table_entries).
                  A caption line whose y0 is BELOW the table's y1 is misplaced.

        Fallback: Original page-height threshold heuristic.
        """
        errors = []

        if self._grobid_table_entries and any(
            e.get("xml_coords") for e in self._grobid_table_entries
        ):
            # ── GROBID path ──────────────────────────────────────────────────
            table_pattern = re.compile(
                r"TABLE\s+([IVXLCDM]+|\d+)[:\.]?\s+([^\n]{10,200})", re.IGNORECASE
            )
            for tbl_entry in self._grobid_table_entries:
                tbl_page = tbl_entry["page"]
                _, tbl_y0, _, tbl_y1 = tbl_entry["bbox"]

                for line_text, line_bbox, page_num in self.line_info:
                    if page_num != tbl_page:
                        continue
                    match = table_pattern.search(line_text)
                    if not match:
                        continue

                    caption_y0 = line_bbox[1]
                    # Caption is below the table's bottom edge → misplaced
                    if caption_y0 > tbl_y1:
                        errors.append(ErrorInstance(
                            check_id=20,
                            check_name="Table Caption Placement",
                            description="Table captions should be placed ABOVE the table, not below",
                            page_num=page_num,
                            text=match.group(0)[:100],
                            bbox=line_bbox,
                            error_type="caption_placement",
                        ))
        else:
            # ── Fallback: page-height heuristic ──────────────────────────────
            table_pattern = re.compile(
                r"TABLE\s+([IVXLCDM]+|\d+)[:\.]?\s+([^\n]{10,200})", re.IGNORECASE
            )
            for line_text, line_bbox, page_num in self.line_info:
                match = table_pattern.search(line_text)
                if match and line_bbox[1] > (2 * 842 / 3):
                    errors.append(ErrorInstance(
                        check_id=20,
                        check_name="Table Caption Placement",
                        description="Table captions should be placed ABOVE the table, not below",
                        page_num=page_num,
                        text=match.group(0)[:100],
                        bbox=line_bbox,
                        error_type="caption_placement",
                    ))

        return errors

    # =========================================================================
    # CHECK #27 — REQUIRED SECTIONS (format-driven, called externally)
    # =========================================================================

    def _check_required_sections(self, required: List[str]) -> List[ErrorInstance]:
        """
        Verify that every section in `required` is present in the document.

        Uses GROBID structural signals for Abstract / Index Terms / References,
        and keyword matching on _grobid_section_heads for all other sections.
        Falls back to full-text regex when GROBID returned no headings.
        """
        if not required:
            return []

        errors = []
        heading_texts = [h["text"].lower() for h in self._grobid_section_heads]

        for section in required:
            found = False
            keywords = SECTION_DETECTION_KEYWORDS.get(section, [section.lower()])

            # Dedicated GROBID signals for the three sections with their own checks
            if section == "Abstract":
                found = self._grobid_has_abstract or bool(
                    re.search(r"\bAbstract\b", self.full_text, re.IGNORECASE)
                )
            elif section == "Index Terms":
                found = self._grobid_has_keywords or bool(
                    re.search(r"Index\s+Terms", self.full_text, re.IGNORECASE)
                )
            elif section == "References":
                found = bool(self.raw_citations) or bool(
                    re.search(r"\bReferences\b", self.full_text, re.IGNORECASE)
                )
            else:
                # Keyword scan against GROBID-extracted headings
                for kw in keywords:
                    if any(kw in heading for heading in heading_texts):
                        found = True
                        break

                # Full-text regex fallback (when GROBID found no headings at all)
                if not found and not self._grobid_section_heads:
                    for kw in keywords:
                        if re.search(r'\b' + re.escape(kw) + r'\b',
                                     self.full_text, re.IGNORECASE):
                            found = True
                            break

            if not found:
                errors.append(ErrorInstance(
                    check_id=27,
                    check_name=f"Required Section Missing: {section}",
                    description=(
                        f"The required section '{section}' was not found in the document. "
                        "Ensure this section is present and clearly labelled."
                    ),
                    page_num=0,
                    text=f"[Section '{section}' not found]",
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
        """Write highlight annotations for every ErrorInstance and save to output_path."""
        color_map = {
            "missing_abstract":          (1.00, 0.70, 0.70),
            "missing_index_terms":       (1.00, 0.80, 0.60),
            "missing_references":        (1.00, 0.85, 0.60),
            "non_roman_heading":         (0.90, 0.90, 0.50),
            "missing_introduction":      (1.00, 0.70, 0.70),
            "non_ieee_citation":         (1.00, 0.75, 0.75),
            "non_ieee_reference_format":    (0.85, 0.95, 1.00),
            "invalid_figure_label":        (0.95, 0.85, 1.00),
            "invalid_table_numbering":     (0.80, 0.95, 0.85),
            "equation_numbering":          (1.00, 0.90, 0.70),
            "figure_numbering_sequence":   (0.95, 0.80, 0.95),
            "table_numbering_sequence":    (0.80, 0.95, 0.90),
            "reference_numbering_sequence":(0.85, 0.85, 1.00),
            "broken_url":                  (1.00, 0.85, 0.85),
            "broken_doi":                  (1.00, 0.85, 0.85),
            "metadata_incomplete":         (1.00, 0.75, 0.55),
            "abstract_word_count":         (0.90, 0.75, 1.00),
            "missing_required_section":    (1.00, 0.65, 0.65),
        }

        for error in errors:
            page = doc[error.page_num]
            color = color_map.get(error.error_type, (1.00, 1.00, 0.60))
            hl = page.add_highlight_annot(error.bbox)
            hl.set_colors(stroke=color)
            hl.set_opacity(0.5)
            hl.info["title"]   = f"Check #{error.check_id}: {error.check_name}"
            hl.info["content"] = f"{error.description}\n\nFound: '{error.text}'"
            hl.update()

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
) -> Tuple[List[ErrorInstance], str, Dict, Dict, Dict]:
    """
    Full pipeline: open PDF → detect errors → annotate → save.

    Args:
        input_path          – path to the source PDF
        output_path         – path where the annotated PDF is written
        required_sections   – sections that must exist (format-driven)
        enabled_check_types – set of error_type strings to keep; None = keep all
        start_page          – 1-indexed page to begin processing from (skips earlier pages)

    Returns:
        errors             – list of ErrorInstance objects (filtered)
        output_path        – path to the annotated PDF
        statistics         – document statistics dict
        extracted_data     – raw extracted text and line data
        reference_analysis – reference quality analysis from external API
    """
    detector = PDFErrorDetector(start_page=start_page)
    errors, doc, statistics = detector.detect_errors(input_path, required_sections)

    # Apply format whitelist: keep only errors whose type is enabled
    if enabled_check_types is not None:
        errors = [e for e in errors if e.error_type in enabled_check_types]

    detector.annotate_pdf(doc, errors, output_path)
    extracted_data = detector.export_extracted_data()
    return errors, output_path, statistics, extracted_data, detector.reference_analysis