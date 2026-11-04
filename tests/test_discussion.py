"""Discussing food with Claude: replies to the review list, and questioning logged entries."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

from gwc.coach import chat, llm, parse, prompts, proposals
from gwc.coach.llm import LLMResult
from gwc.coach.tools import FoodLookup, find_chain_source
from gwc.db.migrate import upgrade
from gwc.db.models import ChainSource, CoachProposal, Food, FoodLog, FoodSource, LogSource
from gwc.db.session import session_scope
from gwc.nutrition.usda import UsdaHit
from gwc.web.components import describe

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 3, 23, 0, tzinfo=UTC)
BIG = {
    "kcal": 1390,
    "protein_g": 64,
    "carbs_g": 108,
    "fat_g": 80,
    "fiber_g": 12,
    "sugar_g": 13,
    "sodium_mg": 4000,
}
CALC = "https://restaurant.nutritionix.com/potbelly/landing"
ETM = "https://www.eatthismuch.com/calories/sicilian-bigs-2277826"


class Backend:
    name = "fake"

    def __init__(self, respond):
        self.respond, self.requests = respond, []

    def run(self, req):
        self.requests.append(req)
        return self.respond(req)


def use(*replies) -> Backend:
    """Answer each request with the next of `replies` (the structured answer, or a text)."""
    queue = list(replies)

    def respond(_req):
        reply = queue.pop(0)
        if isinstance(reply, str):
            return LLMResult(text=reply, model="fake")
        return LLMResult(text="", data=reply, model="fake")

    backend = Backend(respond)
    llm.set_backend_override(backend)
    return backend


def answer(items=(), message=None, question=None, remove=(), notes=None) -> dict:
    return {
        "items": list(items),
        "message": message,
        "question": question,
        "remove_keys": list(remove),
        "research_notes": notes,
    }


def sandwich(**fields) -> dict:
    return {
        "description": "big sicilian",
        "source": "restaurant",
        "food_id": None,
        "fdc_id": None,
        "amount_label": "1 sandwich",
        "grams": 500,
        "eaten_at": None,
        "estimate": None,
        "restaurant": {
            "chain": "Potbelly",
            "item": "Sicilian, BIGS",
            "source_kind": "third_party",
            "source_url": ETM,
            "serving_label": "1 sandwich",
            "serving_grams": 500,
            "nutrients": BIG,
            "official_url": CALC,
            "official_readable": False,
        },
        "basis": "Listed as Sicilian, BIGS",
        "replaces_key": None,
        "override": None,
        **fields,
    }


def first_turn(*items, **fields) -> dict:
    """The review's state after Claude's first answer."""
    use(answer(items, **fields))
    result = parse.parse_description("big sicilian from potbelly", NY, NOW)
    data = describe.append_rows(None, result["items"], replace_origin="parse")
    data.update(
        message=result["message"],
        question=result["question"],
        thread=result["thread"],
        chat=result["chat"],
    )
    return data


@pytest.fixture
def db(isolated_settings):
    upgrade()


def test_a_question_comes_back_with_tappable_options(db):
    question = {"text": "Which size?", "options": ["Skinny", "Original", "Big", "x", "too many"]}
    data = first_turn(question=question)
    assert data["items"] == []
    assert data["question"] == {"text": "Which size?", "options": question["options"][:4]}
    assert data["chat"] == [{"role": "assistant", "text": "Which size?"}]
    bubbles, buttons, display = describe.render_discussion(data)
    assert len(bubbles) == 1 and len(buttons) == 4 and display == "flex"
    # A blank question isn't one.
    assert parse._question({"question": {"text": " ", "options": ["a"]}}) is None


def test_discussion_hidden_without_a_conversation():
    assert describe.render_discussion(None) == ([], [], "none")
    assert describe.render_discussion({"items": [{"key": 0}]}) == ([], [], "none")


def test_reply_sees_the_list_and_earlier_research(db):
    data = first_turn(sandwich(), message="Using a third-party copy.", notes="Original is 980.")
    edits = {"0": {"grams": 250, "include": True}}
    backend = use(answer(message="1,390 fits: the Original alone is 980."))
    result = parse.reply_to_review(
        data["thread"], data["chat"], "Potbelly's app says 895", data["items"], edits, NY, NOW
    )
    (req,) = backend.requests
    assert req.web and req.system == prompts.FOOD_PARSER
    assert [m["role"] for m in req.messages] == ["user", "assistant", "user"]
    assert "My research notes: Original is 980." in req.messages[1]["content"]
    last = req.messages[-1]["content"]
    assert "Potbelly's app says 895" in last
    listed = json.loads(last.split("The review list now:\n")[1].split("\n\nMy reply:")[0])
    assert listed[0]["key"] == 0 and listed[0]["grams"] == 250  # the user's edit, not the original
    assert listed[0]["nutrients"]["kcal"] == pytest.approx(695)
    assert listed[0]["source_kind"] == "third_party" and listed[0]["official_url"] == CALC
    # Its later turns remember only the reply, not the whole list each time.
    assert result["thread"][-2] == {"role": "user", "content": "Potbelly's app says 895"}
    assert [m["role"] for m in result["chat"]] == ["assistant", "user", "assistant"]
    # Answering a question leaves the list exactly as it was, edits included.
    assert describe.apply_reply(data, result)["items"] == data["items"]


def test_reply_changes_only_what_it_names():
    data = {
        "items": [{"key": 0, "name": "a"}, {"key": 1, "name": "b"}, {"key": 5, "name": "scan"}],
        "thread": [],
    }
    result = {
        "items": [
            {"key": 0, "name": "b2", "replaces_key": 1},
            {"key": 1, "name": "new", "replaces_key": None},
            {"key": 2, "name": "bad reference", "replaces_key": 99},
        ],
        "remove_keys": [0],
        "message": "Done",
        "question": None,
        "thread": ["t"],
        "chat": ["c"],
    }
    out = describe.apply_reply(data, result)
    assert [(r["key"], r["name"]) for r in out["items"]] == [
        (6, "b2"),  # in the old row's place, under a new key so old edits don't apply
        (5, "scan"),
        (7, "new"),
        (8, "bad reference"),
    ]
    assert out["message"] == "Done" and out["thread"] == ["t"]


def test_reply_keeps_earlier_usda_matches_and_times(db):
    hit = UsdaHit(
        fdc_id=42,
        name="Egg",
        brand=None,
        data_type="Survey",
        per100=dict.fromkeys(BIG, 10.0),
        servings=[],
    )
    row = {
        "key": 3,
        "name": "Egg",
        "grams": 50,
        "hit": hit.to_dict(),
        "per100": hit.per100,
        "eaten_at": "2026-11-03T08:15:00-05:00",
        "amount_label": "1 egg",
        "source": "usda",
    }
    twice = {
        "description": "2 eggs",
        "source": "usda",
        "food_id": None,
        "fdc_id": 42,
        "amount_label": "2 eggs",
        "grams": 100,
        "eaten_at": None,
        "estimate": None,
        "restaurant": None,
        "basis": None,
        "replaces_key": 3,
        "override": None,
    }
    backend = use(answer([twice]))
    result = parse.reply_to_review(
        [{"role": "user", "content": "1 egg"}, {"role": "assistant", "content": "ok"}],
        [],
        "make it two",
        [row],
        {},
        NY,
        NOW,
        photo="data:image/jpeg;base64,/9j/AAAA",
    )
    (new,) = result["items"]
    assert new.get("error") is None and new["hit"]["fdc_id"] == 42
    assert new["eaten_at"] == row["eaten_at"]  # a changed item stays at the same time
    assert new["photo"] and len(backend.requests[0].images) == 1  # the photo goes along


def test_long_threads_stay_alternating():
    thread = [{"role": "user" if i % 2 == 0 else "assistant", "content": str(i)} for i in range(42)]
    trimmed = parse._trim_thread(thread)
    assert len(trimmed) <= parse.MAX_THREAD_MESSAGES
    assert trimmed[0] == thread[0] and trimmed[-1] == thread[-1]
    roles = [m["role"] for m in trimmed]
    # The turn added next is the user's, so the kept part must end on the assistant.
    assert all(a != b for a, b in zip(roles, roles[1:], strict=False))


def test_claude_can_log_the_users_number_and_both_are_kept(db):
    data = first_turn(sandwich())
    mine = sandwich(
        source="user",
        estimate={**BIG, "kcal": 895},
        replaces_key=0,
        basis="Other nutrients scaled from the published numbers",
        override={
            "said_source": "the Potbelly Android app",
            "published_kcal": 1390,
            "published_source": "eatthismuch.com",
        },
    )
    use(answer([mine], message="Using your 895."))
    result = parse.reply_to_review(
        data["thread"], data["chat"], "Use 895 anyway", data["items"], {}, NY, NOW
    )
    out = describe.apply_reply(data, result)
    (row,) = out["items"]
    assert row["source"] == "user" and row["editable"] and row["key"] == 1
    assert describe._override_line(row).children == (
        "Your numbers, from the Potbelly Android app. Published: 1,390 kcal (eatthismuch.com)."
    )
    assert describe.prefill(row)["kcal"] == pytest.approx(895)
    totals = {**BIG, "kcal": 895}
    with session_scope() as s:
        parse.confirm_items(s, [{**row, "totals": totals, "save": True}], NY)
    with session_scope() as s:
        entry = s.query(FoodLog).one()
        assert entry.kcal == pytest.approx(895)
        assert "Your numbers (895 kcal) from the Potbelly Android app" in entry.note
        assert "published 1,390 kcal (eatthismuch.com)" in entry.note
        food = s.query(Food).one()
        assert "published 1,390 kcal" in food.source_note and food.source_url is None
    # Later lookups can see these are the user's numbers.
    (hit,) = FoodLookup().search_my_foods({"query": "sicilian"})["foods"]
    assert "Your numbers" in hit["source_note"]


def test_use_my_own_numbers_button(db, monkeypatch):
    data = first_turn(sandwich())
    edits = {"0": {"grams": 250, "include": True}}
    monkeypatch.setattr(
        describe,
        "ctx",
        NS(triggered=[{"value": 1}], triggered_id={"type": "ai-override", "index": 0}),
    )
    monkeypatch.setattr(
        "gwc.web.components.common.ctx",
        NS(triggered=[{"value": 1}], triggered_id={"type": "ai-override", "index": 0}),
    )
    (row,) = describe.use_own_numbers([1], data, edits)["items"]
    assert row["editable"] and row["source"] == "user" and row["grams"] == 250
    assert row["override"]["published_kcal"] == pytest.approx(695)
    assert row["override"]["published_source"] == "eatthismuch.com"
    assert describe.prefill(row)["kcal"] == pytest.approx(695)  # starts from what it replaces
    assert describe.SOURCE_LABELS["user"] == "Your numbers"


def test_library_food_overridden_stops_following_the_record(db):
    with session_scope() as s:
        food = Food(source=FoodSource.CUSTOM, name="Soup", **{k: 10.0 for k in BIG})
        s.add(food)
        s.flush()
        row = {
            "key": 0,
            "source": "my_food",
            "food_id": food.id,
            "per100": {k: 10.0 for k in BIG},
            "grams": 100,
            "name": "Soup",
            "amount_label": "100 g",
            "eaten_at": NOW.isoformat(),
        }
    mine = parse.override_row(row)
    assert mine["food_id"] is None
    with session_scope() as s:
        parse.confirm_items(s, [{**mine, "totals": dict.fromkeys(BIG, 5.0)}], NY)
    with session_scope() as s:
        entry = s.query(FoodLog).one()
        assert entry.food_id is None and entry.kcal == 5


def test_published_rows_keep_their_evidence(db):
    data = first_turn(sandwich())
    (row,) = data["items"]
    assert row["basis"] == "Listed as Sicilian, BIGS"
    assert parse.provenance(row) == (
        "Third-party nutrition: eatthismuch.com · Listed as Sicilian, BIGS"
    )
    line = describe._source_line(row)
    hrefs = [c.href for c in line.children if hasattr(c, "href")]
    assert hrefs == [ETM, CALC]  # the third-party copy, and the official page to compare
    official = {**row, "restaurant": {**row["restaurant"], "source_kind": "official"}}
    assert parse.provenance(official).startswith("Official nutrition:")
    with session_scope() as s:
        parse.confirm_items(s, [{**row, "save": True}], NY)
    with session_scope() as s:
        assert "Third-party nutrition: eatthismuch.com" in s.query(FoodLog).one().note
        assert s.query(Food).one().source_note.startswith("Third-party")


def test_chain_sources_are_remembered(db):
    assert find_chain_source({"chain": "Potbelly"}) == {"known": False}
    first_turn(sandwich())
    assert find_chain_source({"chain": " potbelly "}) == {
        "known": True,
        "url": CALC,
        "readable_by_web_tools": False,
    }
    from gwc.nutrition import chains

    with session_scope() as s:
        chains.remember(s, "Potbelly", "https://www.potbelly.com/guess", False)  # keeps the known
        known = chains.find(s, "Potbelly")
        assert known.url == CALC
        chains.remember(s, "Potbelly", "https://www.potbelly.com/menu.pdf", True)  # a better one
        chains.remember(s, "Potbelly", "https://other.example/blocked", False)  # doesn't replace it
        chains.remember(s, "Potbelly", "not a url", True)
        chains.remember(s, " ", "https://example.com", True)
    with session_scope() as s:
        (known,) = s.query(ChainSource).all()
        assert known.url.endswith("menu.pdf") and known.readable
    names = [t.name for t in FoodLookup().specs()]
    assert "find_chain_source" in names


def _entry(s, food_id=None, kcal=1390.0) -> int:
    entry = FoodLog(
        eaten_at=NOW,
        tz="America/New_York",
        food_id=food_id,
        description="Sicilian, BIGS",
        grams=500,
        amount=500,
        source=LogSource.AI,
        note="From: big sicilian",
        **{**BIG, "kcal": kcal},
    )
    s.add(entry)
    s.flush()
    return entry.id


def test_coach_questions_an_entry_and_only_the_user_can_change_it(db):
    with session_scope() as s:
        entry_id = _entry(s)
    backend = use("Let me check.")
    conv_id, job = chat.send(None, "This looks high", NY, NOW, food_log_id=entry_id)
    while job.running:
        pass
    (req,) = backend.requests
    assert req.web
    assert f'"entry_id": {entry_id}' in req.messages[-1]["content"]
    assert "This looks high" in req.messages[-1]["content"]
    assert chat.conversation_entry(conv_id) == entry_id
    tools = {t.name: t for t in req.tools}
    assert {"propose_entry_change", "find_chain_source", "get_food_entries"} <= set(tools)
    assert not any(n.startswith(("apply", "update", "set_", "delete")) for n in tools)

    made = json.loads(
        tools["propose_entry_change"].call(
            {"entry_id": entry_id, "kcal": 895, "reason": "The app says so", "source": "App"}
        )
    )
    assert "Nothing has changed" in made["status"]
    with session_scope() as s:
        assert s.get(FoodLog, entry_id).kcal == 1390  # still unchanged

    (view,) = proposals.for_conversation(conv_id)
    assert view.status == "pending" and proposals.describe_changes(view) == "1,390 → 895 kcal"

    # A newer suggestion replaces the open one.
    second = {"entry_id": entry_id, "kcal": 900, "protein_g": 40, "reason": "Rechecked"}
    second_id = json.loads(tools["propose_entry_change"].call(second))["proposal_id"]
    (view,) = proposals.for_conversation(conv_id)
    assert view.id == second_id and view.changes == {"kcal": 900, "protein_g": 40}

    proposals.apply(second_id)
    with session_scope() as s:
        entry = s.get(FoodLog, entry_id)
        assert entry.kcal == 900 and entry.protein_g == 40 and entry.fat_g == 80
        assert "From: big sicilian" in entry.note
        assert "Changed 1,390 → 900 kcal, 64 → 40 g protein: Rechecked" in entry.note
    with pytest.raises(ValueError):
        proposals.apply(second_id)  # only once
    (line,) = proposals.context_lines(conv_id)
    assert "applied" in line
    (view,) = proposals.for_conversation(conv_id)  # still reads as what changed, once applied
    assert proposals.describe_changes(view) == "1,390 → 900 kcal, 64 → 40 g protein"


@pytest.mark.parametrize(
    "bad",
    [
        {"entry_id": 999, "kcal": 1, "reason": "x"},
        {"kcal": 1, "reason": "x"},
        {"entry_id": 1, "kcal": -1, "reason": "x"},
        {"entry_id": 1, "kcal": 1, "reason": " "},
        {"entry_id": 1, "kcal": 1390, "reason": "same as now"},
        {"entry_id": 1, "reason": "no numbers"},
    ],
)
def test_bad_proposals_are_refused(db, bad):
    with session_scope() as s:
        assert _entry(s) == 1
        from gwc.db.models import CoachConversation

        s.add(CoachConversation(title="t"))
        s.flush()
    with pytest.raises(ValueError):
        proposals.create(1, bad)
    with session_scope() as s:
        assert s.query(CoachProposal).count() == 0


def _proposal(entry_id, library=True) -> int:
    from gwc.db.models import CoachConversation

    with session_scope() as s:
        conv = CoachConversation(title="t")
        s.add(conv)
        s.flush()
        conv_id = conv.id
    return proposals.create(
        conv_id,
        {"entry_id": entry_id, "kcal": 895, "reason": "Rechecked", "update_library": library},
    )["proposal_id"]


def test_apply_can_correct_the_library_food_too(db):
    with session_scope() as s:
        food = Food(source=FoodSource.CUSTOM, name="Sicilian", kcal=278.0)
        s.add(food)
        s.flush()
        food_id = food.id
        entry_id = _entry(s, food_id)
    proposal = _proposal(entry_id)
    proposals.apply(proposal)
    with session_scope() as s:
        assert s.get(Food, food_id).kcal == pytest.approx(179)
        assert "Rechecked" in s.get(Food, food_id).source_note
        assert s.get(FoodLog, entry_id).food_id == food_id


def test_apply_without_library_keeps_the_numbers_when_rescaled(db):
    from gwc.nutrition import log

    with session_scope() as s:
        food = Food(source=FoodSource.CUSTOM, name="Sicilian", kcal=278.0)
        s.add(food)
        s.flush()
        food_id = food.id
        entry_id = _entry(s, food_id)
    proposal = _proposal(entry_id, library=False)
    proposals.apply(proposal)
    with session_scope() as s:
        assert s.get(Food, food_id).kcal == 278  # the food is untouched
        entry = s.get(FoodLog, entry_id)
        assert entry.food_id is None
        log.update_entry(
            s,
            entry_id,
            amount=250,
            unit="g",
            eaten_at=NOW,
            food_day_override=None,
            note=None,
        )
        assert entry.kcal == pytest.approx(447.5)  # scaled from 895, not recomputed from the food


def test_usda_records_are_never_changed(db):
    with session_scope() as s:
        food = Food(source=FoodSource.USDA, fdc_id=7, name="Egg", kcal=100.0)
        s.add(food)
        s.flush()
        food_id = food.id
        entry_id = _entry(s, food_id)
    proposal = _proposal(entry_id, library=True)
    with session_scope() as s:
        assert s.get(CoachProposal, proposal).update_library is False
    proposals.apply(proposal)
    with session_scope() as s:
        assert s.get(Food, food_id).kcal == 100
        assert s.get(FoodLog, entry_id).food_id is None


def test_dismissing_leaves_the_entry_alone(db):
    with session_scope() as s:
        entry_id = _entry(s)
    proposal = _proposal(entry_id)
    proposals.dismiss(proposal)
    with session_scope() as s:
        assert s.get(FoodLog, entry_id).kcal == 1390
        assert s.get(CoachProposal, proposal).status == "dismissed"
    with pytest.raises(ValueError):
        proposals.apply(proposal)


def test_deleted_entry_is_handled(db):
    with session_scope() as s:
        entry_id = _entry(s)
    proposal = _proposal(entry_id)
    with session_scope() as s:
        s.delete(s.get(FoodLog, entry_id))
    with pytest.raises(ValueError):
        proposals.apply(proposal)
    assert chat.entry_note(9999, NY) == "[The entry this conversation was about has been deleted.]"
    assert chat.entry_note(None, NY) is None
