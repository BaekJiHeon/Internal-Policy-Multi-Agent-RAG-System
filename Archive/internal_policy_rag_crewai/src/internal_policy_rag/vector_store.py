"""Chroma 기반 사내 규정 영속 Vector Store.

규정 원문은 로컬에서 임베딩하고, Chroma에는 벡터·원문 chunk·검색용
metadata를 함께 저장한다. 파일 해시가 바뀌지 않은 문서는 재파싱과
재임베딩을 건너뛴다.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .models import PolicyChunk


ACCESS_RANK = {"ALL": 0, "INTERNAL": 1, "CONFIDENTIAL": 2}
TOKEN_PATTERN = re.compile(r"[가-힣A-Za-z0-9]+")
COLLECTION_NAME = "internal_policies"
INDEX_SCHEMA_VERSION = "1"


class EmbeddingError(RuntimeError):
    """로컬 임베딩 생성 실패."""


class VectorStoreError(RuntimeError):
    """Vector DB 초기화 또는 동기화 실패."""


def _stable_index(feature: str, dimensions: int) -> int:
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % dimensions


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _corpus_id(directory: Path) -> str:
    normalized = unicodedata.normalize("NFC", str(directory.resolve()))
    digest = hashlib.blake2b(normalized.encode("utf-8"), digest_size=8).hexdigest()
    return f"corpus-{digest}"


class LocalHashEmbedding:
    """외부 API 없이 실행되는 한국어 규정용 경량 로컬 임베딩."""

    def __init__(self, dimensions: int = 2048) -> None:
        if dimensions < 128:
            raise ValueError("dimensions는 128 이상이어야 합니다.")
        self.dimensions = dimensions

    @property
    def identifier(self) -> str:
        return f"local-hash-blake2-{self.dimensions}-v1"

    @staticmethod
    def _tokens(text: str) -> list[str]:
        normalized = unicodedata.normalize("NFKC", text).lower()
        return TOKEN_PATTERN.findall(normalized)

    def embed(self, text: str) -> dict[int, float]:
        if not isinstance(text, str) or not text.strip():
            raise EmbeddingError("빈 문자열은 임베딩할 수 없습니다.")

        tokens = self._tokens(text)
        if not tokens:
            raise EmbeddingError("임베딩할 수 있는 토큰이 없습니다.")

        features: Counter[str] = Counter()
        for token in tokens:
            features[f"w:{token}"] += 1.0
            compact = re.sub(r"\s+", "", token)
            for size, weight in ((2, 0.30), (3, 0.45)):
                if len(compact) >= size:
                    for start in range(len(compact) - size + 1):
                        features[f"c{size}:{compact[start:start + size]}"] += weight
        for left, right in zip(tokens, tokens[1:]):
            features[f"b:{left}|{right}"] += 0.7

        vector: Counter[int] = Counter()
        for feature, weight in features.items():
            vector[_stable_index(feature, self.dimensions)] += weight

        norm = math.sqrt(sum(value * value for value in vector.values()))
        if norm == 0:
            raise EmbeddingError("임베딩 벡터의 크기가 0입니다.")
        return {index: value / norm for index, value in vector.items()}

    def embed_dense(self, text: str) -> list[float]:
        sparse = self.embed(text)
        dense = [0.0] * self.dimensions
        for index, value in sparse.items():
            dense[index] = value
        return dense


def cosine_similarity(left: dict[int, float], right: dict[int, float]) -> float:
    """기존 호출부와 회귀 테스트를 위한 희소 벡터 유사도 함수."""

    if len(left) > len(right):
        left, right = right, left
    return max(
        0.0,
        sum(value * right.get(index, 0.0) for index, value in left.items()),
    )


@dataclass(frozen=True)
class VectorSearchHit:
    chunk: PolicyChunk
    score: float


@dataclass(frozen=True)
class IndexSyncResult:
    added: int
    updated: int
    deleted: int
    unchanged: int
    total: int
    parsed_files: int
    reused_files: int

    def model_dump(self) -> dict[str, int]:
        return asdict(self)


class PolicyVectorStore:
    """하나의 Chroma collection에서 corpus와 권한 metadata를 필터링한다."""

    def __init__(
        self,
        chunks: Iterable[PolicyChunk] | None = None,
        embedding: LocalHashEmbedding | None = None,
        *,
        persist_directory: str | Path | None = None,
        corpus_id: str = "memory",
        collection_name: str = COLLECTION_NAME,
    ) -> None:
        self.embedding = embedding or LocalHashEmbedding()
        self.persist_directory = (
            Path(persist_directory).resolve() if persist_directory else None
        )
        self.corpus_id = corpus_id
        self.collection_name = (
            collection_name
            if self.persist_directory is not None
            else f"{collection_name}_{uuid.uuid4().hex}"
        )

        try:
            import chromadb
            from chromadb.config import Settings
        except ImportError as exc:
            raise VectorStoreError(
                "영속 Vector DB를 사용하려면 chromadb>=1.0,<2.0이 필요합니다."
            ) from exc

        settings = Settings(anonymized_telemetry=False)
        try:
            if self.persist_directory is None:
                self.client = chromadb.EphemeralClient(settings=settings)
            else:
                self.persist_directory.mkdir(parents=True, exist_ok=True)
                self.client = chromadb.PersistentClient(
                    path=str(self.persist_directory),
                    settings=settings,
                )
            self.collection = self.client.get_or_create_collection(
                name=self.collection_name,
                embedding_function=None,
                configuration={"hnsw": {"space": "cosine"}},
                metadata={
                    "description": "사내 규정 조항 공용 collection",
                    "embedding_id": self.embedding.identifier,
                    "index_schema_version": INDEX_SCHEMA_VERSION,
                },
            )
        except Exception as exc:
            location = self.persist_directory or "메모리"
            raise VectorStoreError(
                f"Chroma Vector DB 초기화 실패({location}): {exc}"
            ) from exc

        self.last_sync = IndexSyncResult(0, 0, 0, 0, 0, 0, 0)
        if chunks is not None:
            chunk_list = list(chunks)
            if not chunk_list:
                raise ValueError("Vector Store를 만들 정책 chunk가 없습니다.")
            self.last_sync = self.sync_chunks(chunk_list)

    @classmethod
    def from_directory(
        cls,
        policy_dir: str | Path,
        *,
        persist_directory: str | Path,
        embedding: LocalHashEmbedding | None = None,
        force: bool = False,
    ) -> "PolicyVectorStore":
        directory = Path(policy_dir).resolve()
        store = cls(
            embedding=embedding,
            persist_directory=persist_directory,
            corpus_id=_corpus_id(directory),
        )
        store.sync_directory(directory, force=force)
        return store

    @staticmethod
    def _embedding_text(chunk: PolicyChunk) -> str:
        return " ".join(
            [
                chunk.document_name,
                chunk.document_type,
                chunk.chapter,
                chunk.article,
                chunk.content,
            ]
        )

    def _record_id(self, chunk_id: str) -> str:
        return f"{self.corpus_id}:{chunk_id}"

    def _record_hash(self, chunk: PolicyChunk, source_hash: str) -> str:
        payload = {
            "chunk": chunk.model_dump(mode="json"),
            "source_hash": source_hash,
            "embedding_id": self.embedding.identifier,
            "index_schema_version": INDEX_SCHEMA_VERSION,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _metadata(
        self,
        chunk: PolicyChunk,
        *,
        source_hash: str,
        record_hash: str,
    ) -> dict[str, str]:
        return {
            "corpus_id": self.corpus_id,
            "chunk_id": chunk.chunk_id,
            "document_name": chunk.document_name,
            "document_type": chunk.document_type,
            "department": chunk.department,
            "chapter": chunk.chapter,
            "article": chunk.article,
            "effective_date": chunk.effective_date,
            "version": chunk.version,
            "status": chunk.status,
            "access_level": chunk.access_level,
            "source_file": chunk.source_file,
            "source_hash": source_hash,
            "record_hash": record_hash,
            "embedding_id": self.embedding.identifier,
            "index_schema_version": INDEX_SCHEMA_VERSION,
        }

    @staticmethod
    def _chunk_from_record(
        metadata: dict[str, Any], document: str | None
    ) -> PolicyChunk:
        return PolicyChunk(
            chunk_id=str(metadata["chunk_id"]),
            document_name=str(metadata["document_name"]),
            document_type=str(metadata["document_type"]),
            department=str(metadata["department"]),
            chapter=str(metadata["chapter"]),
            article=str(metadata["article"]),
            effective_date=str(metadata["effective_date"]),
            version=str(metadata["version"]),
            status=str(metadata["status"]),
            access_level=str(metadata["access_level"]),
            source_file=str(metadata["source_file"]),
            content=document or "",
        )

    def _get_corpus_records(self) -> dict[str, tuple[dict[str, Any], str]]:
        result = self.collection.get(
            where={"corpus_id": {"$eq": self.corpus_id}},
            include=["documents", "metadatas"],
        )
        ids = result.get("ids") or []
        metadatas = result.get("metadatas") or []
        documents = result.get("documents") or []
        return {
            record_id: (metadata or {}, document or "")
            for record_id, metadata, document in zip(ids, metadatas, documents)
        }

    def sync_directory(
        self,
        policy_dir: str | Path,
        *,
        force: bool = False,
    ) -> tuple[list[PolicyChunk], IndexSyncResult]:
        """정책 폴더의 변경분만 파싱·임베딩해 현재 corpus와 동기화한다."""

        from .parser import find_policy_documents, parse_policy_document

        directory = Path(policy_dir).resolve()
        if not directory.exists():
            raise FileNotFoundError(f"정책 문서 폴더를 찾을 수 없습니다: {directory}")
        files = find_policy_documents(directory)
        if not files:
            raise FileNotFoundError(
                f"Markdown, PDF, HWP 또는 HWPX 정책 문서가 없습니다: {directory}"
            )

        source_hashes = {path.name: _file_hash(path) for path in files}
        existing = self._get_corpus_records()
        existing_by_source: dict[str, list[tuple[dict[str, Any], str]]] = {}
        for metadata, document in existing.values():
            source_file = str(metadata.get("source_file", ""))
            existing_by_source.setdefault(source_file, []).append(
                (metadata, document)
            )

        chunks: list[PolicyChunk] = []
        parsed_files = 0
        reused_files = 0
        for path in files:
            records = existing_by_source.get(path.name, [])
            reusable = (
                not force
                and bool(records)
                and all(
                    metadata.get("source_hash") == source_hashes[path.name]
                    and metadata.get("embedding_id") == self.embedding.identifier
                    and metadata.get("index_schema_version")
                    == INDEX_SCHEMA_VERSION
                    for metadata, _ in records
                )
            )
            if reusable:
                chunks.extend(
                    self._chunk_from_record(metadata, document)
                    for metadata, document in records
                )
                reused_files += 1
            else:
                chunks.extend(parse_policy_document(path))
                parsed_files += 1

        result = self.sync_chunks(
            chunks,
            source_hashes=source_hashes,
            force=force,
            parsed_files=parsed_files,
            reused_files=reused_files,
        )
        return self.list_chunks(), result

    def sync_chunks(
        self,
        chunks: Iterable[PolicyChunk],
        *,
        source_hashes: dict[str, str] | None = None,
        force: bool = False,
        parsed_files: int = 0,
        reused_files: int = 0,
    ) -> IndexSyncResult:
        """전달된 chunk를 upsert하고 corpus에서 사라진 레코드를 제거한다."""

        chunk_list = list(chunks)
        if not chunk_list:
            raise ValueError("Vector Store를 만들 정책 chunk가 없습니다.")

        source_hashes = source_hashes or {}
        existing = self._get_corpus_records()
        desired: dict[
            str, tuple[PolicyChunk, dict[str, str], str]
        ] = {}
        for chunk in chunk_list:
            source_hash = source_hashes.get(chunk.source_file, "")
            record_hash = self._record_hash(chunk, source_hash)
            metadata = self._metadata(
                chunk,
                source_hash=source_hash,
                record_hash=record_hash,
            )
            desired[self._record_id(chunk.chunk_id)] = (
                chunk,
                metadata,
                record_hash,
            )

        added = 0
        updated = 0
        unchanged = 0
        to_upsert: list[tuple[str, PolicyChunk, dict[str, str]]] = []
        for record_id, (chunk, metadata, record_hash) in desired.items():
            previous = existing.get(record_id)
            if previous is None:
                added += 1
                to_upsert.append((record_id, chunk, metadata))
            elif force or previous[0].get("record_hash") != record_hash:
                updated += 1
                to_upsert.append((record_id, chunk, metadata))
            else:
                unchanged += 1

        try:
            for start in range(0, len(to_upsert), 100):
                batch = to_upsert[start : start + 100]
                self.collection.upsert(
                    ids=[item[0] for item in batch],
                    embeddings=[
                        self.embedding.embed_dense(
                            self._embedding_text(item[1])
                        )
                        for item in batch
                    ],
                    documents=[item[1].content for item in batch],
                    metadatas=[item[2] for item in batch],
                )

            stale_ids = sorted(set(existing) - set(desired))
            for start in range(0, len(stale_ids), 500):
                self.collection.delete(ids=stale_ids[start : start + 500])
        except Exception as exc:
            raise VectorStoreError(f"Chroma 인덱스 동기화 실패: {exc}") from exc

        self.last_sync = IndexSyncResult(
            added=added,
            updated=updated,
            deleted=len(stale_ids),
            unchanged=unchanged,
            total=len(desired),
            parsed_files=parsed_files,
            reused_files=reused_files,
        )
        return self.last_sync

    def list_chunks(self) -> list[PolicyChunk]:
        records = self._get_corpus_records()
        chunks = [
            self._chunk_from_record(metadata, document)
            for metadata, document in records.values()
        ]
        return sorted(
            chunks,
            key=lambda item: (
                item.source_file,
                item.chapter,
                item.article,
                item.chunk_id,
            ),
        )

    @staticmethod
    def _is_accessible(document_level: str, user_level: str) -> bool:
        document_rank = ACCESS_RANK.get(document_level)
        user_rank = ACCESS_RANK.get(user_level)
        return (
            document_rank is not None
            and user_rank is not None
            and document_rank <= user_rank
        )

    @staticmethod
    def _where_filter(
        *,
        corpus_id: str,
        access_level: str,
        document_types: list[str] | None,
    ) -> dict[str, Any]:
        allowed_levels = [
            level
            for level, rank in ACCESS_RANK.items()
            if rank <= ACCESS_RANK[access_level]
        ]
        filters: list[dict[str, Any]] = [
            {"corpus_id": {"$eq": corpus_id}},
            {"status": {"$eq": "active"}},
            {"access_level": {"$in": allowed_levels}},
        ]
        if document_types:
            filters.append(
                {"document_type": {"$in": sorted(set(document_types))}}
            )
        return {"$and": filters}

    def search(
        self,
        query: str,
        *,
        document_types: list[str] | None = None,
        access_level: str = "ALL",
        top_k: int = 5,
        min_score: float = 0.12,
    ) -> list[VectorSearchHit]:
        if top_k < 1 or top_k > 20:
            raise ValueError("top_k는 1~20 범위여야 합니다.")
        if access_level not in ACCESS_RANK:
            raise ValueError(f"지원하지 않는 접근 등급입니다: {access_level}")

        try:
            result = self.collection.query(
                query_embeddings=[self.embedding.embed_dense(query)],
                n_results=top_k,
                where=self._where_filter(
                    corpus_id=self.corpus_id,
                    access_level=access_level,
                    document_types=document_types,
                ),
                include=["documents", "metadatas", "distances"],
            )
        except Exception as exc:
            raise VectorStoreError(f"Chroma 정책 검색 실패: {exc}") from exc

        documents = (result.get("documents") or [[]])[0] or []
        metadatas = (result.get("metadatas") or [[]])[0] or []
        distances = (result.get("distances") or [[]])[0] or []
        hits: list[VectorSearchHit] = []
        for document, metadata, distance in zip(
            documents, metadatas, distances
        ):
            if not metadata:
                continue
            # DB filter에 더해 신뢰 경계에서 권한을 한 번 더 확인한다.
            if metadata.get("status") != "active":
                continue
            if not self._is_accessible(
                str(metadata.get("access_level", "")), access_level
            ):
                continue
            score = max(0.0, min(1.0, 1.0 - float(distance)))
            if score < min_score:
                continue
            hits.append(
                VectorSearchHit(
                    chunk=self._chunk_from_record(metadata, document),
                    score=score,
                )
            )
        return hits
