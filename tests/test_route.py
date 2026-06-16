from abenlux import route


def _body(text, model="claude-opus-4-8", max_tokens=512, tools=None):
    b = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": text}]}
    if tools:
        b["tools"] = tools
    return b


def test_easy_request_routes_to_cheaper_model():
    d = route.decide(_body("please rename the foo variable to bar", max_tokens=16), "anthropic")
    assert d.target == "claude-haiku-4-5"
    assert d.original == "claude-opus-4-8"


def test_real_work_stays_on_the_strong_model():
    big = "design a distributed consensus protocol with leader election and recovery " * 30
    d = route.decide(_body(big), "anthropic")
    assert d.target is None


def test_a_tool_call_is_never_downrouted():
    d = route.decide(_body("rename foo to bar", tools=[{"name": "edit"}]), "anthropic")
    assert d.target is None


def test_an_already_cheap_model_is_left_alone():
    d = route.decide(_body("rename foo to bar", model="claude-haiku-4-5"), "anthropic")
    assert d.target is None


def test_openai_easy_request_routes():
    d = route.decide(_body("fix the import for os", model="gpt-4o"), "openai")
    assert d.target == "gpt-4o-mini"


def test_saving_is_positive_and_in_the_right_direction():
    s = route.saving_usd("claude-opus-4-8", "claude-haiku-4-5", 5000, 500)
    assert s > 0
    # routing the other way is never a saving
    assert route.saving_usd("claude-haiku-4-5", "claude-opus-4-8", 5000, 500) == 0.0


def test_difficulty_marks_easy_low_and_hard_high():
    easy = route.difficulty(_body("add a docstring", max_tokens=16), "anthropic")
    hard = route.difficulty(_body("x" * 9000), "anthropic")
    assert easy < 0.2
    assert hard == 1.0
