"""Document endpoints. Ownership/visibility is enforced in document_service."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, get_provider, get_session, get_settings_dep
from app.config import Settings
from app.db import Database
from app.models import User
from app.schemas import ChunkOut, DocumentOut
from app.services import document_service
from app.services.llm_provider import LLMProvider

router = APIRouter(prefix="/documents", tags=["documents"])
Session = Annotated[AsyncSession, Depends(get_session)]
CurrentUser = Annotated[User, Depends(get_current_user)]


@router.get("")
async def list_documents(session: Session, user: CurrentUser) -> list[DocumentOut]:
    docs = await document_service.list_documents(session, user)
    return [DocumentOut.model_validate(d) for d in docs]


@router.post("", status_code=201)
async def upload_document(
    file: Annotated[UploadFile, File()],
    session: Session,
    user: CurrentUser,
    db: Annotated[Database, Depends(get_db)],
    provider: Annotated[LLMProvider, Depends(get_provider)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> DocumentOut:
    doc = await document_service.upload_document(
        session, db.session_factory, provider, settings, user, file
    )
    return DocumentOut.model_validate(doc)


@router.delete("/{document_id}", status_code=204)
async def delete_document(document_id: uuid.UUID, session: Session, user: CurrentUser) -> Response:
    await document_service.delete_document(session, user, document_id)
    return Response(status_code=204)


@router.get("/{document_id}/chunks/{chunk_id}")
async def get_chunk(
    document_id: uuid.UUID, chunk_id: int, session: Session, user: CurrentUser
) -> ChunkOut:
    return await document_service.get_chunk(session, user, document_id, chunk_id)
