"""Coach chat: persisted conversations; each reply runs as a background job."""

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from gwc.analysis.units import weight_unit
from gwc.coach import prompts
from gwc.coach.jobs import Job, get_jobs
from gwc.coach.llm import LLMRequest, get_backend
from gwc.coach.settings import feature_config
from gwc.coach.tools import data_tools
from gwc.db.models import CoachConversation, CoachMessage
from gwc.db.session import session_scope
from gwc.db.types import utcnow

HISTORY_MESSAGES = 30  # older turns are left out of the prompt


def list_conversations() -> list[CoachConversation]:
    with session_scope() as s:
        return list(
            s.scalars(select(CoachConversation).order_by(CoachConversation.updated_at.desc()))
        )


def messages(conversation_id: int) -> list[CoachMessage]:
    with session_scope() as s:
        return list(
            s.scalars(
                select(CoachMessage)
                .where(CoachMessage.conversation_id == conversation_id)
                .order_by(CoachMessage.id)
            )
        )


def delete_conversation(conversation_id: int) -> None:
    with session_scope() as s:
        conv = s.get(CoachConversation, conversation_id)
        if conv is not None:
            s.delete(conv)


def _title(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= 60 else line[:57] + "…"


def context_note(tz: ZoneInfo, now: datetime) -> str:
    local = now.astimezone(tz)
    return f"[Local time {local:%A %Y-%m-%d %H:%M}. Weight unit: {weight_unit()}.]"


def send(conversation_id: int | None, text: str, tz: ZoneInfo, now: datetime) -> tuple[int, Job]:
    """Store the user's message and start the reply job; returns (conversation id, job)."""
    with session_scope() as s:
        conv = s.get(CoachConversation, conversation_id) if conversation_id else None
        if conv is None:
            conv = CoachConversation(title=_title(text))
            s.add(conv)
            s.flush()
        s.add(CoachMessage(conversation_id=conv.id, role="user", content=text.strip()))
        conv.updated_at = utcnow()
        conv_id = conv.id

    def run(job: Job) -> str:
        history = [
            {"role": m.role, "content": m.content} for m in messages(conv_id)[-HISTORY_MESSAGES:]
        ]
        history[-1]["content"] = f"{context_note(tz, now)}\n\n{history[-1]['content']}"
        req = LLMRequest(
            system=prompts.COACH,
            messages=history,
            config=feature_config("chat"),
            tools=data_tools(),
            on_text=job.on_text,
            on_turn=job.on_turn,
            on_activity=job.on_activity,
        )
        result = get_backend().run(req)
        with session_scope() as s:
            s.add(
                CoachMessage(
                    conversation_id=conv_id,
                    role="assistant",
                    content=result.text.strip() or "(no reply)",
                    model=result.model,
                )
            )
            conv = s.get(CoachConversation, conv_id)
            if conv is not None:
                conv.updated_at = utcnow()
        return result.text

    return conv_id, get_jobs().start("chat", run, key=f"chat:{conv_id}")
