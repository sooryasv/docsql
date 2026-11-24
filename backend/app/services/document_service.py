"""PDF validation, page-aware chunking, atomic indexing, and ownership-checked document access."""

import asyncio
import hashlib
import re
import uuid
from dataclasses import dataclass
from io import BytesIO

import structlog
from fastapi import UploadFile
from pypdf import PdfReader
from pypdf.errors import PyPdfError
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from app.config import Settings
from app.errors import (
    DocumentLimitError,
    DuplicateDocumentError,
    FileTooLargeError,
    InvalidPdfError,
    NotFoundError,
    ScannedPdfUnsupportedError,
    SeedDocumentProtectedError,
    TooManyPagesError,
)
from app.models import Document, DocumentChunk, User
from app.schemas import ChunkOut
from app.services.llm_provider import LLMProvider

log = structlog.get_logger()
MAX_BYTES = 5 * 1024 * 1024
MAX_PAGES = 15
READ_BLOCK = 64 * 1024
CHUNK_CHARS = 900
OVERLAP_CHARS = 150
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class ChunkDraft:
    page_number: int
    chunk_index: int
    content: str


async def read_upload(file: UploadFile) -> bytes:
    """Bounded read: never trusts Content-Length; rejects at MAX_BYTES + 1."""
    data = bytearray()
    while block := await file.read(READ_BLOCK):
        data.extend(block)
        if len(data) > MAX_BYTES:
            raise FileTooLargeError(f"File exceeds {MAX_BYTES // (1024 * 1024)} MB")
    return bytes(data)


def _extract_sync(data: bytes) -> list[str]:
    if not data.startswith(b"%PDF-"):
        raise InvalidPdfError("File does not start with a PDF header")
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise InvalidPdfError("Encrypted PDFs are not supported")
        if len(reader.pages) > MAX_PAGES:  # checked BEFORE any text extraction
            raise TooManyPagesError(f"PDF has more than {MAX_PAGES} pages")
        pages = [(page.extract_text() or "").replace("\x00", "") for page in reader.pages]
    except (PyPdfError, ValueError, KeyError, TypeError, OSError, RecursionError) as exc:
        raise InvalidPdfError("PDF could not be parsed") from exc
    if not any(p.strip() for p in pages):
        raise ScannedPdfUnsupportedError()
    return pages


async def extract_pages(data: bytes, timeout_s: float) -> list[str]:
    """Runs in a thread under a deadline. A timed-out thread is abandoned, not killed."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(_extract_sync, data), timeout_s)
    except TimeoutError as exc:
        raise InvalidPdfError("PDF parsing timed out") from exc


def _pieces(text_block: str) -> list[str]:
    """Paragraphs, then sentences; any sentence still over the limit is split on word boundaries."""
    pieces: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text_block):
        for sentence in _SENTENCE_SPLIT.split(" ".join(paragraph.split())):
            words = sentence.split(" ")
            current = ""
            for word in words:
                if current and len(current) + 1 + len(word) > CHUNK_CHARS:
                    pieces.append(current)
                    current = word
                else:
                    current = f"{current} {word}".strip()
            if current:
                pieces.append(current)
    return pieces


def split_page(page_text: str) -> list[str]:
    chunks: list[str] = []
    current = ""
    for piece in _pieces(page_text):
        if current and len(current) + 1 + len(piece) > CHUNK_CHARS:
            chunks.append(current)
            tail = current[-OVERLAP_CHARS:]
            tail = tail[tail.find(" ") + 1 :] if " " in tail else tail  # start on a word boundary
            current = f"{tail} {piece}".strip()
        else:
            current = f"{current} {piece}".strip()
    if current:
        chunks.append(current)
    return chunks


def chunk_pages(pages: list[str]) -> list[ChunkDraft]:
    """Chunks never cross a page boundary; chunk_index is global within the document."""
    drafts: list[ChunkDraft] = []
    for page_number, page_text in enumerate(pages, start=1):
        for content in split_page(page_text):
            drafts.append(ChunkDraft(page_number, len(drafts), content))
    return drafts


async def index_pages(
    session_factory: async_sessionmaker[AsyncSession],
    provider: LLMProvider,
    settings: Settings,
    *,
    pages: list[str],
    title: str,
    filename: str,
    file_sha256: str,
    uploaded_by: uuid.UUID | None,
    is_system_seed: bool,
) -> Document:
    """Shared by uploads and seeding. Embeddings are computed BEFORE any DB write; one transaction."""
    drafts = chunk_pages(pages)
    if not drafts:
        raise ScannedPdfUnsupportedError()
    vectors = await provider.embed_documents([d.content for d in drafts])
    document = Document(
        uploaded_by=uploaded_by,
        title=title[:200],
        filename=filename[:255],
        file_sha256=file_sha256,
        page_count=len(pages),
        chunk_count=len(drafts),
        embedding_model=provider.embedding_model,
        is_system_seed=is_system_seed,
    )
    document.chunks = [
        DocumentChunk(
            page_number=d.page_number, chunk_index=d.chunk_index, content=d.content, embedding=v
        )
        for d, v in zip(drafts, vectors, strict=True)
    ]
    async with session_factory() as session:
        try:
            if uploaded_by is not None:  # serialize per-user so the document cap cannot be raced
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:u, 0))"),
                    {"u": str(uploaded_by)},
                )
                owned = await session.scalar(
                    select(func.count())
                    .select_from(Document)
                    .where(Document.uploaded_by == uploaded_by)
                )
                if (owned or 0) >= settings.max_docs_per_user:
                    raise DocumentLimitError(
                        f"Limit of {settings.max_docs_per_user} documents reached"
                    )
            session.add(document)
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise DuplicateDocumentError() from exc
    return document


async def upload_document(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    provider: LLMProvider,
    settings: Settings,
    user: User,
    file: UploadFile,
) -> Document:
    data = await read_upload(file)
    pages = await extract_pages(data, settings.pdf_parse_timeout_s)
    digest = hashlib.sha256(data).hexdigest()
    existing = await session.scalar(
        select(Document.id).where(Document.uploaded_by == user.id, Document.file_sha256 == digest)
    )
    if existing is not None:
        raise DuplicateDocumentError()
    owned = await session.scalar(
        select(func.count()).select_from(Document).where(Document.uploaded_by == user.id)
    )
    if (owned or 0) >= settings.max_docs_per_user:
        raise DocumentLimitError(f"Limit of {settings.max_docs_per_user} documents reached")
    filename = (file.filename or "upload.pdf").rsplit("/", 1)[-1]
    title = (
        re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE).replace("_", " ").strip() or "Untitled"
    )
    return await index_pages(
        session_factory,
        provider,
        settings,
        pages=pages,
        title=title,
        filename=filename,
        file_sha256=digest,
        uploaded_by=user.id,
        is_system_seed=False,
    )


def _visible(user: User) -> ColumnElement[bool]:
    return or_(Document.is_system_seed, Document.uploaded_by == user.id)


async def list_documents(session: AsyncSession, user: User) -> list[Document]:
    stmt = (
        select(Document)
        .where(_visible(user))
        .order_by(Document.is_system_seed.desc(), Document.created_at.desc())
    )
    return list((await session.scalars(stmt)).all())


async def _visible_document(session: AsyncSession, user: User, document_id: uuid.UUID) -> Document:
    document = await session.scalar(
        select(Document).where(Document.id == document_id, _visible(user))
    )
    if document is None:  # 404, never 403: other users' documents are not enumerable
        raise NotFoundError("Document not found")
    return document


async def delete_document(session: AsyncSession, user: User, document_id: uuid.UUID) -> None:
    document = await _visible_document(session, user, document_id)
    if document.is_system_seed:
        raise SeedDocumentProtectedError()
    await session.delete(document)
    await session.commit()


async def get_chunk(
    session: AsyncSession, user: User, document_id: uuid.UUID, chunk_id: int
) -> ChunkOut:
    document = await _visible_document(session, user, document_id)
    chunk = await session.scalar(
        select(DocumentChunk).where(
            DocumentChunk.id == chunk_id, DocumentChunk.document_id == document.id
        )
    )
    if chunk is None:  # chunk of a different document => same 404
        raise NotFoundError("Chunk not found")
    return ChunkOut(
        id=chunk.id,
        document_id=document.id,
        title=document.title,
        page_number=chunk.page_number,
        content=chunk.content,
    )
