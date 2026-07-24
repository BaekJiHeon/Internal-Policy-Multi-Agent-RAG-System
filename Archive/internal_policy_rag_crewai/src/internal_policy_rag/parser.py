"""Markdown/PDF/HWP/HWPX 규정을 장 → 조 또는 의미 절 단위로 파싱한다."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

from .hwp import extract_hwp_lines, extract_hwpx_lines
from .models import PolicyChunk


class PolicyDocumentError(ValueError):
    """규정 문서 형식 또는 추출 품질이 올바르지 않을 때 발생한다."""


REQUIRED_METADATA = {
    "document_name",
    "document_type",
    "department",
    "version",
    "effective_date",
    "status",
    "access_level",
}

CHAPTER_PATTERN = re.compile(r"^#\s+(제\s*\d+\s*장.*)$")
ARTICLE_PATTERN = re.compile(r"^##\s+(제\s*\d+\s*조(?:의\s*\d+)?(?:\([^)]*\))?.*)$")
EXTRACTED_CHAPTER_PATTERN = re.compile(r"^제\s*(\d+)\s*장\s*(.*)$")
EXTRACTED_ARTICLE_PATTERN = re.compile(
    r"^제\s*(\d+)\s*조(?:의\s*(\d+))?\s*(?:\(([^)]{1,80})\))?\s*(.*)$"
)
SUPPORTED_POLICY_SUFFIXES = (".hwp", ".hwpx", ".md", ".pdf")

# 파일명에서만 안전하게 확인할 수 있는 문서 metadata다. 실제 운영에서는
# DMS의 승인 metadata 또는 별도 manifest로 교체해야 한다.
POLICY_PROFILES = (
    ("병가", "병가 등 휴가 운영방법", "휴가", "총무부"),
    ("시간외근무", "시간외근무 실시기준", "시간외근무", "총무부"),
    ("여비규정", "여비규정", "출장·여비", "총무부"),
    ("취업규칙", "취업규칙", "인사·복무", "총무부"),
    ("복무규정", "복무규정", "인사·복무", "총무부"),
)

SEMANTIC_SECTION_NAMES = {
    "병가",
    "병가의종류",
    "병가일수의계산",
    "병가의운영방법",
    "병가관련q&a",
    "장기재직휴가",
    "장기재직휴가대상및기간",
    "임신검진동행휴가",
    "임신검진동행휴가대상및기간",
    "신청및사용방법",
}


def _parse_front_matter(text: str, source: Path) -> tuple[dict[str, str], list[str]]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise PolicyDocumentError(f"{source.name}: YAML 형식의 front matter가 없습니다.")

    try:
        closing = next(
            index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"
        )
    except StopIteration as exc:
        raise PolicyDocumentError(f"{source.name}: front matter 종료 구분자가 없습니다.") from exc

    metadata: dict[str, str] = {}
    for line in lines[1:closing]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise PolicyDocumentError(f"{source.name}: 잘못된 metadata 행: {line}")
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip("\"'")

    missing = sorted(REQUIRED_METADATA - metadata.keys())
    if missing:
        raise PolicyDocumentError(
            f"{source.name}: 필수 metadata 누락: {', '.join(missing)}"
        )
    return metadata, lines[closing + 1 :]


def _make_chunk(
    source: Path,
    metadata: dict[str, str],
    chapter: str,
    article: str,
    article_lines: list[str],
) -> PolicyChunk:
    content = "\n".join(line for line in article_lines).strip()
    if not content:
        raise PolicyDocumentError(f"{source.name}: 빈 조항 chunk가 생성되었습니다.")
    identity = (
        f"{source.name}|{metadata['version']}|{chapter}|{article}|{content[:240]}"
    )
    digest = hashlib.blake2b(identity.encode("utf-8"), digest_size=8).hexdigest()
    return PolicyChunk(
        chunk_id=f"CH-{digest}",
        document_name=metadata["document_name"],
        document_type=metadata["document_type"],
        department=metadata["department"],
        chapter=chapter,
        article=article,
        effective_date=metadata["effective_date"],
        version=metadata["version"],
        status=metadata["status"].lower(),
        access_level=metadata["access_level"].upper(),
        source_file=source.name,
        content=content,
    )


def parse_markdown_policy_document(path: str | Path) -> list[PolicyChunk]:
    """Markdown 문서를 조 단위 chunk 목록으로 변환한다."""

    source = Path(path)
    metadata, body_lines = _parse_front_matter(
        source.read_text(encoding="utf-8"), source
    )
    chunks: list[PolicyChunk] = []
    chapter = "장 정보 없음"
    article = ""
    article_lines: list[str] = []

    def flush() -> None:
        nonlocal article_lines
        if article and any(line.strip() for line in article_lines):
            chunks.append(
                _make_chunk(source, metadata, chapter, article, article_lines)
            )
        article_lines = []

    for line in body_lines:
        chapter_match = CHAPTER_PATTERN.match(line.strip())
        article_match = ARTICLE_PATTERN.match(line.strip())

        if chapter_match:
            flush()
            chapter = chapter_match.group(1).strip()
            article = ""
            continue
        if article_match:
            flush()
            article = article_match.group(1).strip()
            article_lines = [line.strip()]
            continue
        if article:
            article_lines.append(line)

    flush()
    if not chunks:
        raise PolicyDocumentError(f"{source.name}: '제N조' 형식의 조항이 없습니다.")
    return chunks


def _filename_date(stem: str) -> str:
    normalized = unicodedata.normalize("NFKC", stem)
    korean_date = re.search(
        r"(20\d{2})\s*년도?\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일",
        normalized,
    )
    if korean_date:
        year, month, day = map(int, korean_date.groups())
        return f"{year:04d}-{month:02d}-{day:02d}"
    dotted_date = re.search(
        r"(?<!\d)(\d{2,4})\s*[.]\s*(\d{1,2})\s*[.]\s*(\d{1,2})",
        normalized,
    )
    if dotted_date:
        year, month, day = map(int, dotted_date.groups())
        year = year + 2000 if year < 100 else year
        return f"{year:04d}-{month:02d}-{day:02d}"
    raise PolicyDocumentError(f"{stem}: 파일명에서 제·개정일을 찾을 수 없습니다.")


def _source_metadata(source: Path) -> dict[str, str]:
    normalized_name = unicodedata.normalize("NFKC", source.stem).replace(" ", "")
    for keyword, document_name, document_type, department in POLICY_PROFILES:
        if keyword.replace(" ", "") in normalized_name:
            effective_date = _filename_date(normalized_name)
            return {
                "document_name": document_name,
                "document_type": document_type,
                "department": department,
                "version": effective_date,
                "effective_date": effective_date,
                "status": "active",
                # 원본에 접근등급 metadata가 없으므로 데모 기본값을 사용한다.
                # 운영에서는 DMS metadata로 반드시 덮어써야 한다.
                "access_level": "ALL",
            }
    raise PolicyDocumentError(f"{source.name}: 지원하지 않는 규정 파일명입니다.")


def _normalize_extracted_line(line: str) -> str:
    value = unicodedata.normalize("NFKC", line)
    value = re.sub(r"\(cid:\d+\)", "·", value)
    value = value.replace("\u00a0", " ")
    value = re.sub(r"[ \t]+", " ", value).strip()
    # 시간외근무 기준 PDF에서 제목의 마지막 글자와 바로 뒤 인용 규정명이
    # 글꼴 순서 때문에 교차 추출되는 패턴을 원문 시각 확인 결과에 맞게 복원한다.
    value = re.sub(
        r"사전허「가복\)\s*무\s*규정」",
        "사전허가) 「복무규정」",
        value,
    )
    value = value.replace(
        "근무시 근무를 명하거나",
        "근무시간외 근무를 명하거나",
    )
    value = re.sub(r"^제\s+(\d+)\s*장", r"제\1장", value)
    value = re.sub(r"^제\s+(\d+)\s*조", r"제\1조", value)
    return value


def _extract_pdf_lines(source: Path) -> list[str]:
    try:
        import pdfplumber
    except ImportError as exc:
        raise PolicyDocumentError(
            "PDF 규정 처리를 위해 pdfplumber>=0.11이 필요합니다."
        ) from exc

    lines: list[str] = []
    pages_with_text = 0
    try:
        with pdfplumber.open(source) as pdf:
            page_count = len(pdf.pages)
            for page in pdf.pages:
                text = page.extract_text(
                    x_tolerance=2,
                    y_tolerance=3,
                    layout=False,
                ) or ""
                page_lines = [
                    _normalize_extracted_line(line)
                    for line in text.splitlines()
                    if _normalize_extracted_line(line)
                ]
                page_lines = [
                    line
                    for line in page_lines
                    if not re.fullmatch(r"-?\s*\d+\s*-?", line)
                ]
                if page_lines:
                    pages_with_text += 1
                    lines.extend(page_lines)
    except Exception as exc:
        raise PolicyDocumentError(f"{source.name}: PDF 텍스트 추출 실패: {exc}") from exc

    if not lines or pages_with_text < max(1, page_count // 2):
        raise PolicyDocumentError(
            f"{source.name}: 텍스트 추출률이 낮아 OCR 검토가 필요합니다."
        )
    return lines


def _parse_extracted_articles(
    source: Path,
    metadata: dict[str, str],
    lines: list[str],
) -> list[PolicyChunk]:
    chunks: list[PolicyChunk] = []
    chapter = "장 정보 없음"
    article = ""
    article_lines: list[str] = []

    def flush() -> None:
        nonlocal article_lines
        if article and any(line.strip() for line in article_lines):
            chunks.append(
                _make_chunk(source, metadata, chapter, article, article_lines)
            )
        article_lines = []

    for line in lines:
        compact = line.replace(" ", "")
        if compact in {"부칙", "附則"}:
            flush()
            chapter = "부칙"
            article = ""
            continue

        chapter_match = EXTRACTED_CHAPTER_PATTERN.match(line)
        if chapter_match:
            flush()
            chapter = f"제{chapter_match.group(1)}장 {chapter_match.group(2)}".strip()
            article = ""
            continue

        article_match = EXTRACTED_ARTICLE_PATTERN.match(line)
        if article_match:
            flush()
            number, sub_number, title, remainder = article_match.groups()
            article = f"제{number}조"
            if sub_number:
                article += f"의{sub_number}"
            if title:
                article += f"({title.strip()})"
            article_lines = [article]
            if remainder.strip():
                article_lines.append(remainder.strip())
            continue

        if article:
            article_lines.append(line)

    flush()
    return [
        chunk
        for chunk in chunks
        if "<삭제>" not in chunk.content.replace(" ", "")
        and (
            not chunk.content
            or chunk.content.count("·") / max(len(chunk.content), 1) < 0.12
        )
    ]


def _semantic_heading(line: str) -> bool:
    compact = (
        line.replace(" ", "")
        .replace("<", "")
        .replace(">", "")
        .replace(":", "")
        .lower()
    )
    compact = re.sub(r"^\d+[.)]?", "", compact)
    if compact.startswith("참고병가관련q&a"):
        return True
    return compact in SEMANTIC_SECTION_NAMES


def _parse_extracted_semantic_sections(
    source: Path,
    metadata: dict[str, str],
    lines: list[str],
) -> list[PolicyChunk]:
    chunks: list[PolicyChunk] = []
    section = ""
    section_lines: list[str] = []

    def flush() -> None:
        nonlocal section_lines
        content_length = len("".join(section_lines).replace(" ", ""))
        if section and content_length >= 20:
            chunks.append(
                _make_chunk(
                    source,
                    metadata,
                    "운영방법",
                    section,
                    section_lines,
                )
            )
        section_lines = []

    for line in lines:
        if _semantic_heading(line):
            flush()
            section = line
            section_lines = [line]
            continue
        if section:
            section_lines.append(line)

    flush()
    return chunks


def _parse_extracted_policy_document(
    source: Path, lines: list[str]
) -> list[PolicyChunk]:
    metadata = _source_metadata(source)
    normalized_lines = [
        normalized
        for line in lines
        if (normalized := _normalize_extracted_line(line))
    ]
    chunks = _parse_extracted_articles(source, metadata, normalized_lines)
    if not chunks:
        chunks = _parse_extracted_semantic_sections(
            source, metadata, normalized_lines
        )
    if not chunks:
        raise PolicyDocumentError(
            f"{source.name}: 조항 또는 의미 절을 찾지 못해 수동 검토가 필요합니다."
        )
    return chunks


def parse_pdf_policy_document(path: str | Path) -> list[PolicyChunk]:
    """PDF 규정을 조 단위로, 조가 없으면 의미 절 단위로 청킹한다."""

    source = Path(path)
    return _parse_extracted_policy_document(source, _extract_pdf_lines(source))


def parse_hwp_policy_document(path: str | Path) -> list[PolicyChunk]:
    """HWP 5.x 규정을 로컬 추출한 뒤 공통 규정 구조로 청킹한다."""

    source = Path(path)
    return _parse_extracted_policy_document(source, extract_hwp_lines(source))


def parse_hwpx_policy_document(path: str | Path) -> list[PolicyChunk]:
    """HWPX XML 규정을 추출한 뒤 공통 규정 구조로 청킹한다."""

    source = Path(path)
    return _parse_extracted_policy_document(source, extract_hwpx_lines(source))


def parse_policy_document(path: str | Path) -> list[PolicyChunk]:
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"규정 문서를 찾을 수 없습니다: {source}")
    suffix = source.suffix.lower()
    if suffix == ".md":
        return parse_markdown_policy_document(source)
    if suffix == ".pdf":
        return parse_pdf_policy_document(source)
    if suffix == ".hwp":
        return parse_hwp_policy_document(source)
    if suffix == ".hwpx":
        return parse_hwpx_policy_document(source)
    raise PolicyDocumentError(f"지원하지 않는 문서 형식입니다: {source.suffix}")


def find_policy_documents(directory: Path) -> list[Path]:
    """지원하는 정책 원본만 현재 폴더에서 이름순으로 찾는다."""

    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_POLICY_SUFFIXES
    )


def load_policy_chunks(policy_dir: str | Path) -> list[PolicyChunk]:
    directory = Path(policy_dir)
    if not directory.exists():
        raise FileNotFoundError(f"정책 문서 폴더를 찾을 수 없습니다: {directory}")

    files = find_policy_documents(directory)
    if not files:
        raise FileNotFoundError(
            f"Markdown, PDF, HWP 또는 HWPX 정책 문서가 없습니다: {directory}"
        )

    chunks: list[PolicyChunk] = []
    for path in files:
        chunks.extend(parse_policy_document(path))
    return chunks
