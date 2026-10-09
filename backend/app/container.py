"""Composition root: builds every process-wide dependency exactly once.

Nothing else in the application constructs clients or services; routes receive
them from here, and tests pass fakes for the external clients into `build`.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agent.service import AgentService
from app.core.config import Settings
from app.db.pool import Database
from app.db.repositories.circuit_breakers import PostgresBreakerStore
from app.llm.groq_client import GroqClient
from app.memory.client import HindsightGateway
from app.services.feedback_service import FeedbackService
from app.services.file_service import FileService
from app.services.resolution_service import ResolutionService
from app.services.webhook_service import WebhookService
from app.storage.supabase_storage import SupabaseStorage


@dataclass(frozen=True)
class Container:
    settings: Settings
    db: Database
    storage: SupabaseStorage
    hindsight: HindsightGateway
    groq: GroqClient
    agent: AgentService
    feedback: FeedbackService
    resolutions: ResolutionService
    files: FileService
    webhooks: WebhookService

    @classmethod
    def build(
        cls,
        settings: Settings,
        *,
        db: Database,
        storage: SupabaseStorage,
        hindsight: HindsightGateway,
        groq: GroqClient,
    ) -> Container:
        bank_id = settings.hindsight_bank_id
        return cls(
            settings=settings,
            db=db,
            storage=storage,
            hindsight=hindsight,
            groq=groq,
            agent=AgentService(settings=settings, db=db, storage=storage, hindsight=hindsight, groq=groq),
            feedback=FeedbackService(db, bank_id=bank_id),
            resolutions=ResolutionService(db, bank_id=bank_id),
            files=FileService(db, storage, settings),
            webhooks=WebhookService(db, hindsight, settings),
        )

    @classmethod
    async def create(cls, settings: Settings) -> Container:
        """Production wiring with real clients."""
        db = Database(settings, role="api")
        await db.open()
        breakers = PostgresBreakerStore(db)
        return cls.build(
            settings,
            db=db,
            storage=SupabaseStorage(settings),
            hindsight=HindsightGateway(settings, breaker_store=breakers),
            groq=GroqClient(settings, breaker_store=breakers),
        )

    async def close(self) -> None:
        await self.groq.close()
        await self.hindsight.close()
        await self.storage.close()
        await self.db.close()
