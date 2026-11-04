"""Changes to logged entries that the coach suggests and only the user can apply.

The coach gets one tool, which records a proposal. Nothing touches the food log
until the user presses Apply on its card, and the entry's note keeps the old numbers.
"""

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from gwc.coach.llm import ToolSpec
from gwc.db.models import CoachProposal, Food, FoodLog, FoodSource
from gwc.db.session import session_scope
from gwc.db.types import utcnow
from gwc.nutrition.nutrients import NUTRIENT_KEYS


@dataclass
class ProposalView:
    id: int
    entry_id: int | None
    entry_name: str | None
    before: dict[str, float]
    changes: dict[str, float]
    reason: str
    source: str | None
    update_library: bool
    library_food: str | None
    status: str


def create(conversation_id: int, args: dict[str, Any]) -> dict[str, Any]:
    """The propose_entry_change tool: validate and record it, replacing an older pending one."""
    changes = {}
    for key in NUTRIENT_KEYS:
        value = args.get(key)
        if value is None:
            continue
        if float(value) < 0:
            raise ValueError(f"{key} can't be negative")
        changes[key] = float(value)
    reason = (args.get("reason") or "").strip()
    if not reason:
        raise ValueError("Say why this change is right")
    with session_scope() as s:
        entry = s.get(FoodLog, int(args.get("entry_id") or 0))
        if entry is None:
            raise ValueError("No food log entry with that entry_id")
        changes = {k: v for k, v in changes.items() if v != getattr(entry, k)}
        if not changes:
            raise ValueError("Those are already the entry's numbers")
        food = s.get(Food, entry.food_id) if entry.food_id else None
        library = bool(args.get("update_library")) and _can_update(food, entry)
        for old in s.scalars(
            select(CoachProposal).where(
                CoachProposal.food_log_id == entry.id, CoachProposal.status == "pending"
            )
        ):
            old.status = "replaced"
        proposal = CoachProposal(
            conversation_id=conversation_id,
            food_log_id=entry.id,
            changes=json.dumps(changes),
            before=json.dumps({k: getattr(entry, k) for k in changes}),
            reason=reason,
            source=(args.get("source") or "").strip() or None,
            update_library=library,
            status="pending",
        )
        s.add(proposal)
        s.flush()
        return {
            "proposal_id": proposal.id,
            "status": "Shown to the user as a card. Nothing has changed unless they press Apply.",
            "updates_library_food": library,
        }


def _can_update(food: Food | None, entry: FoodLog) -> bool:
    """Only the user's own foods can change; USDA records and recipes keep their numbers."""
    return food is not None and food.source is FoodSource.CUSTOM and entry.grams > 0


def propose_tool(conversation_id: int) -> ToolSpec:
    numbers = {k: {"type": "number", "description": "Total as eaten"} for k in NUTRIENT_KEYS}
    return ToolSpec(
        "propose_entry_change",
        "Suggest new numbers for a logged entry (entry_id from get_food_entries). The user "
        "sees a card and decides: this does not change the log. Give totals as eaten, and "
        "only the nutrients that should change. Set update_library to also correct the saved "
        "food behind the entry (library_food_id), so it's right next time.",
        {
            "type": "object",
            "properties": {
                "entry_id": {"type": "integer"},
                **numbers,
                "reason": {"type": "string", "description": "Why, in a sentence"},
                "source": {"type": "string", "description": "Where the numbers came from"},
                "update_library": {"type": "boolean"},
            },
            "required": ["entry_id", "reason"],
        },
        lambda args: create(conversation_id, args),
        lambda _a: "Writing up a suggested change…",
    )


def for_conversation(conversation_id: int) -> list[ProposalView]:
    with session_scope() as s:
        proposals = s.scalars(
            select(CoachProposal)
            .where(
                CoachProposal.conversation_id == conversation_id,
                CoachProposal.status != "replaced",
            )
            .order_by(CoachProposal.id)
        ).all()
        views = []
        for p in proposals:
            entry = s.get(FoodLog, p.food_log_id) if p.food_log_id else None
            food = s.get(Food, entry.food_id) if entry is not None and entry.food_id else None
            changes = json.loads(p.changes)
            views.append(
                ProposalView(
                    id=p.id,
                    entry_id=entry.id if entry is not None else None,
                    entry_name=entry.description if entry is not None else None,
                    before=json.loads(p.before),
                    changes=changes,
                    reason=p.reason,
                    source=p.source,
                    update_library=p.update_library,
                    library_food=food.name if food is not None else None,
                    status=p.status,
                )
            )
        return views


def _change_words(key: str, before: float, after: float) -> str:
    unit = {"kcal": " kcal", "sodium_mg": " mg sodium"}.get(key, f" g {key.removesuffix('_g')}")
    return f"{before:,.0f} → {after:,.0f}{unit}"


def describe_changes(view: ProposalView) -> str:
    return ", ".join(_change_words(k, view.before.get(k, 0), v) for k, v in view.changes.items())


def apply(proposal_id: int) -> None:
    """Change the entry, noting the old numbers, and its library food if that was proposed.

    Without a library change the entry stops following its food: otherwise editing its
    amount later would recompute it from the food and lose these numbers.
    """
    with session_scope() as s:
        proposal = s.get(CoachProposal, proposal_id)
        if proposal is None or proposal.status != "pending":
            raise ValueError("That suggestion is no longer open")
        entry = s.get(FoodLog, proposal.food_log_id) if proposal.food_log_id else None
        if entry is None:
            raise ValueError("That entry no longer exists")
        changes = json.loads(proposal.changes)
        before = {k: getattr(entry, k) for k in changes}
        proposal.before = json.dumps(before)
        words = ", ".join(_change_words(k, before[k], v) for k, v in changes.items())
        for key, value in changes.items():
            setattr(entry, key, value)
        note = f"Changed {words}: {proposal.reason}"
        if proposal.source:
            note += f" ({proposal.source})"
        entry.note = f"{entry.note} · {note}" if entry.note else note
        food = s.get(Food, entry.food_id) if entry.food_id else None
        if proposal.update_library and _can_update(food, entry):
            for key, value in changes.items():
                setattr(food, key, value * 100 / entry.grams)
            food.source_note = note
        else:
            entry.food_id = None
        proposal.status = "applied"
        proposal.decided_at = utcnow()


def dismiss(proposal_id: int) -> None:
    with session_scope() as s:
        proposal = s.get(CoachProposal, proposal_id)
        if proposal is not None and proposal.status == "pending":
            proposal.status = "dismissed"
            proposal.decided_at = utcnow()


def context_lines(conversation_id: int) -> list[str]:
    """What became of this conversation's suggestions, for Claude's next turn."""
    return [
        f"[Your suggestion for entry {v.entry_id} ({describe_changes(v)}) is {v.status}.]"
        for v in for_conversation(conversation_id)
    ]
