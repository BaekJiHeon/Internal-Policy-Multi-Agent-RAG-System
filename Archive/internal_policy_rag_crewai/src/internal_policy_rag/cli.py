"""명령행 실행기."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

from .evaluation import DEFAULT_TEST_QUESTIONS, run_batch_tests
from .orchestrator import DEFAULT_POLICY_DIR, InternalPolicyRAGSystem
from .rag import DEFAULT_VECTOR_DB_DIR, PolicySearchEngine


def _running_panel(
    question: str,
    agent_name: str = "시스템 준비",
    summary: str = (
        "실행 환경과 규정 검색 인덱스를 확인하고 있습니다.\n"
        "준비가 끝나면 질문 분석을 시작합니다."
    ),
) -> Panel:
    """질문과 실행 상태를 한눈에 보여주는 입력 패널."""

    summary_lines = [
        " ".join(line.split()) for line in summary.splitlines() if line.strip()
    ]
    compact_summary = "\n".join(summary_lines[:2])
    return Panel(
        Group(
            Text(question, style="bold white"),
            Text(""),
            Spinner(
                "dots",
                text=Text(
                    f" 에이전트 실행 중 · {agent_name}",
                    style="bold white",
                ),
                style="bright_white",
            ),
            Text(compact_summary, style="white"),
        ),
        title="[bold white]입력[/bold white]",
        title_align="left",
        border_style="bright_green",
        padding=(1, 2),
    )


def _result_panel(markdown: str) -> Panel:
    """최종 답변을 보라색 테두리로 표시하는 결과 패널."""

    return Panel(
        Markdown(markdown),
        title="[bold bright_magenta]결과[/bold bright_magenta]",
        title_align="left",
        border_style="bright_magenta",
        padding=(1, 2),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="사내 규정 RAG 멀티에이전트 질의응답"
    )
    parser.add_argument("question", nargs="?", help="사내 규정 질문")
    parser.add_argument(
        "--run-tests",
        action="store_true",
        help="기본 평가 시나리오를 CrewAI/OpenAI로 실행",
    )
    parser.add_argument(
        "--policy-dir",
        type=Path,
        default=DEFAULT_POLICY_DIR,
        help="Markdown, PDF, HWP 또는 HWPX 정책 문서 폴더",
    )
    parser.add_argument(
        "--access-level",
        default="ALL",
        choices=["ALL", "INTERNAL", "CONFIDENTIAL"],
    )
    parser.add_argument(
        "--vector-db-dir",
        type=Path,
        default=DEFAULT_VECTOR_DB_DIR,
        help="Chroma 영속 DB 볼륨 폴더",
    )
    parser.add_argument(
        "--index-only",
        action="store_true",
        help="질문하지 않고 규정을 Vector DB에 생성·동기화한 뒤 종료",
    )
    parser.add_argument(
        "--force-reindex",
        action="store_true",
        help="현재 corpus의 모든 규정을 다시 파싱·임베딩",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="단일 질문 결과를 JSON으로 출력",
    )
    return parser


async def _main_async(args: argparse.Namespace) -> int:
    if args.index_only:
        engine = PolicySearchEngine.from_directory(
            args.policy_dir,
            persist_directory=args.vector_db_dir,
            force_reindex=args.force_reindex,
        )
        print(
            json.dumps(
                {
                    "vector_db_dir": str(args.vector_db_dir.resolve()),
                    "collection": engine.store.collection_name,
                    "corpus_id": engine.store.corpus_id,
                    "sync": engine.index_stats.model_dump(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.run_tests:
        system = InternalPolicyRAGSystem(
            policy_dir=args.policy_dir,
            user_context={"access_level": args.access_level},
            vector_db_dir=args.vector_db_dir,
            force_reindex=args.force_reindex,
        )
        rows = await run_batch_tests(system, DEFAULT_TEST_QUESTIONS)
        print(
            json.dumps(
                [row.model_dump() for row in rows],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not args.question:
        raise SystemExit(
            "질문을 입력하거나 --index-only/--run-tests를 사용하세요."
        )
    if args.json:
        system = InternalPolicyRAGSystem(
            policy_dir=args.policy_dir,
            user_context={"access_level": args.access_level},
            vector_db_dir=args.vector_db_dir,
            force_reindex=args.force_reindex,
        )
        result = await system.answer_policy_question(args.question)
        print(result.model_dump_json(indent=2))
    else:
        console = Console()
        with Live(
            _running_panel(args.question),
            console=console,
            refresh_per_second=10,
        ) as live:
            def report_progress(agent_name: str, summary: str) -> None:
                live.update(
                    _running_panel(args.question, agent_name, summary),
                    refresh=True,
                )

            system = InternalPolicyRAGSystem(
                policy_dir=args.policy_dir,
                user_context={"access_level": args.access_level},
                vector_db_dir=args.vector_db_dir,
                force_reindex=args.force_reindex,
                progress_callback=report_progress,
            )
            result = await system.answer_policy_question(args.question)
        console.print()
        console.print(_result_panel(result.final_answer.markdown))
    return 0


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
