"""Coach chat: persisted conversations; each reply runs as a background job."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from gwc.analysis.units import weight_unit
from gwc.coach import prompts, proposals
from gwc.coach.jobs import Job, get_jobs
from gwc.coach.llm import LLMRequest, get_backend
from gwc.coach.settings import feature_config
from gwc.coach.tools import chain_source_tool, data_tools
from gwc.db.models import CoachConversation, CoachMessage, Food, FoodLog
from gwc.db.session import session_scope
from gwc.db.types import utcnow
from gwc.nutrition.nutrients import NUTRIENT_KEYS

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


def entry_note(entry_id: int | None, tz: ZoneInfo) -> str | None:
    """The logged entry a conversation is about, with its numbers as they are now."""
    if entry_id is None:
        return None
    with session_scope() as s:
        entry = s.get(FoodLog, entry_id)
        if entry is None:
            return "[The entry this conversation was about has been deleted.]"
        food = s.get(Food, entry.food_id) if entry.food_id else None
        details = {
            "entry_id": entry.id,
            "food": entry.description,
            "eaten_at": entry.eaten_at.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
            "amount": entry.unit_label or f"{entry.grams:.0f} g",
            "note": entry.note,
            "library_food_id": entry.food_id,
            "library_source_url": food.source_url if food is not None else None,
            **{k: round(getattr(entry, k), 1) for k in NUTRIENT_KEYS},
        }
    return f"[The user is questioning this logged entry: {json.dumps(details)}]"


def conversation_entry(conversation_id: int | None) -> int | None:
    if not conversation_id:
        return None
    with session_scope() as s:
        conv = s.get(CoachConversation, conversation_id)
        return conv.food_log_id if conv is not None else None


def send(
    conversation_id: int | None,
    text: str,
    tz: ZoneInfo,
    now: datetime,
    food_log_id: int | None = None,
) -> tuple[int, Job]:
    """Store the user's message and start the reply job; returns (conversation id, job).

    `food_log_id` starts the conversation about a logged entry (Question this).
    """
    with session_scope() as s:
        conv = s.get(CoachConversation, conversation_id) if conversation_id else None
        if conv is None:
            conv = CoachConversation(title=_title(text), food_log_id=food_log_id)
            s.add(conv)
            s.flush()
        s.add(CoachMessage(conversation_id=conv.id, role="user", content=text.strip()))
        conv.updated_at = utcnow()
        conv_id = conv.id

    def run(job: Job) -> str:
        history = [
            {"role": m.role, "content": m.content} for m in messages(conv_id)[-HISTORY_MESSAGES:]
        ]
        notes = [
            context_note(tz, now),
            entry_note(conversation_entry(conv_id), tz),
            *proposals.context_lines(conv_id),
        ]
        context = "\n".join(n for n in notes if n)
        history[-1]["content"] = f"{context}\n\n{history[-1]['content']}"
        req = LLMRequest(
            system=prompts.COACH,
            messages=history,
            config=feature_config("chat"),
            tools=[*data_tools(), chain_source_tool(), proposals.propose_tool(conv_id)],
            on_text=job.on_text,
            on_turn=job.on_turn,
            on_activity=job.on_activity,
            web=True,  # to check a logged food against its published numbers
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
