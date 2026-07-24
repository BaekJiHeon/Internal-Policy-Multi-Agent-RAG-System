"""한글 HWP/HWPX 원본에서 규정 텍스트를 로컬로 추출한다."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import unicodedata
import zipfile
from pathlib import Path
from xml.etree import ElementTree


SECTION_PATTERN = re.compile(r"^Contents/section(\d+)\.xml$")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _normalize_line(line: str) -> str:
    normalized = unicodedata.normalize("NFKC", line)
    return re.sub(r"[ \t]+", " ", normalized).strip()


def _hwp5txt_command() -> str | None:
    sibling = Path(sys.executable).with_name("hwp5txt")
    if sibling.is_file():
        return str(sibling)
    return shutil.which("hwp5txt")


def extract_hwp_lines(source: Path, *, timeout: int = 60) -> list[str]:
    """pyhwp의 hwp5txt로 HWP 5.x 본문을 로컬 추출한다."""

    from .parser import PolicyDocumentError

    command = _hwp5txt_command()
    if command is None:
        raise PolicyDocumentError(
            f"{source.name}: HWP 처리를 위한 hwp5txt(pyhwp)를 찾을 수 없습니다."
        )

    try:
        result = subprocess.run(
            [command, str(source)],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise PolicyDocumentError(
            f"{source.name}: HWP 텍스트 추출기를 실행하지 못했습니다."
        ) from exc

    if result.returncode != 0:
        raise PolicyDocumentError(
            f"{source.name}: HWP 텍스트 추출 실패"
            f"(종료 코드 {result.returncode})."
        )

    lines = [
        normalized
        for line in result.stdout.splitlines()
        if (normalized := _normalize_line(line))
    ]
    if not lines:
        raise PolicyDocumentError(f"{source.name}: HWP 본문 텍스트가 없습니다.")
    return lines


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    parts: list[str] = []

    def visit(node: ElementTree.Element) -> None:
        for child in node:
            if _local_name(child.tag) == "p":
                continue
            if _local_name(child.tag) == "t" and child.text:
                parts.append(child.text)
            visit(child)

    visit(paragraph)
    return _normalize_line("".join(parts))


def extract_hwpx_lines(source: Path) -> list[str]:
    """HWPX section XML의 문단을 문서 순서대로 추출한다."""

    try:
        with zipfile.ZipFile(source) as archive:
            sections = sorted(
                (
                    (int(match.group(1)), name)
                    for name in archive.namelist()
                    if (match := SECTION_PATTERN.match(name))
                ),
                key=lambda item: item[0],
            )
            if not sections:
                raise ValueError("Contents/section*.xml 본문이 없습니다.")

            lines: list[str] = []
            for _, name in sections:
                root = ElementTree.fromstring(archive.read(name))
                for node in root.iter():
                    if _local_name(node.tag) != "p":
                        continue
                    text = _paragraph_text(node)
                    if text:
                        lines.append(text)
    except (
        OSError,
        ValueError,
        zipfile.BadZipFile,
        ElementTree.ParseError,
    ) as exc:
        from .parser import PolicyDocumentError

        raise PolicyDocumentError(
            f"{source.name}: HWPX 텍스트 추출 실패: {exc}"
        ) from exc

    if not lines:
        from .parser import PolicyDocumentError

        raise PolicyDocumentError(f"{source.name}: HWPX 본문 텍스트가 없습니다.")
    return lines
