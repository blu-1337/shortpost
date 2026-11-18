from shortpost import related

VIDS = [
    {"id": "AAAAAAAAAAA", "title": "Mixing and Mastering with A SINGLE PLUGIN | DOGMA REVIEW", "snippet": "#trap #hiphop #beats Here I am mixing"},
    {"id": "BBBBBBBBBBB", "title": "Can EQ Help You Create BANGERS? | Kirchhoff EQ", "snippet": "#trap #hiphop #beats"},
    {"id": "CCCCCCCCCCC", "title": "Sad Melodic PNL Type Beat | NEVER (Prod. Samastano)", "snippet": "#trap #hiphop #beats"},
]


class FakeLLM:
    def __init__(self, answer):
        self.answer = answer

    def ask(self, system, user, schema, seed, last=False):
        return self.answer


def test_rank_product_name_wins_and_hashtag_boilerplate_ignored():
    core = {"products": ["DOGMA"], "keywords": ["dogma mastering"], "category": "mastering"}
    ranked = related.rank(core, "dogma limiter trick", "", VIDS)
    assert ranked[0][1]["id"] == "AAAAAAAAAAA"
    assert dict((v["id"], s) for s, v in ranked)["CCCCCCCCCCC"] == 0  # '#trap #hiphop #beats' must not count


def test_choose_no_match_returns_none():
    core = {"products": ["Vatrica"], "keywords": ["free enhancer plugin"], "category": "enhancer"}
    assert related.choose({}, core, "vatrica enhancer", "", VIDS, FakeLLM({"video_id": "AAAAAAAAAAA", "reason": "x"})) is None


def test_choose_uses_ai_pick_among_candidates_only():
    core = {"products": ["Kirchhoff EQ"], "keywords": ["eq for 808"], "category": "eq"}
    ok = related.choose({}, core, "kirchhoff eq", "", VIDS, FakeLLM({"video_id": "BBBBBBBBBBB", "reason": "same plugin"}))
    assert ok["id"] == "BBBBBBBBBBB" and ok["by"] == "ai"
    # an invented id falls back to the word match
    bad = related.choose({}, core, "kirchhoff eq", "", VIDS, FakeLLM({"video_id": "ZZZZZZZZZZZ", "reason": "?"}))
    assert bad["id"] == "BBBBBBBBBBB" and bad["by"] == "words"
    # the model may also decline
    assert related.choose({}, core, "kirchhoff eq", "", VIDS, FakeLLM({"video_id": "none", "reason": "no"})) is None
