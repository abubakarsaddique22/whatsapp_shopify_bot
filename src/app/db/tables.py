"""SQLAlchemy tables. Starts empty; each phase adds its tables and an Alembic migration.

Phase 3: catalog mirror | Phase 4: whatsapp_inbox, whatsapp_outbox, conversations, messages
Phase 8: kb_documents, kb_chunks | Phase 10: sync_events, sync_runs | Phase 11: handoffs
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
