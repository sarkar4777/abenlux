from abenlux.teammemory import TeamMemory, detect_language

# a stable made-up embedding and a near copy of it
V = [0.10, 0.20, 0.30, 0.40, 0.50]
V_CLOSE = [0.11, 0.19, 0.31, 0.39, 0.52]
V_FAR = [0.9, -0.4, 0.1, -0.7, 0.2]


def test_the_first_record_in_a_team_has_nothing_to_match():
    tm = TeamMemory()
    assert tm.match("acme", V, "python", "claude-opus-4-8", "alice", 1.0) is None


def test_an_almost_identical_ask_in_the_same_language_can_be_served():
    tm = TeamMemory()
    tm.add("acme", V, "python", "claude-opus-4-8", "alice", 2.0)
    m = tm.match("acme", V, "python", "claude-opus-4-8", "bob", 2.0)
    assert m.tier == "serve"
    assert m.solver == "alice"
    assert m.shadow_usd == 2.0


def test_same_task_in_another_language_is_a_warm_start_not_a_serve():
    tm = TeamMemory()
    tm.add("acme", V, "python", "claude-opus-4-8", "alice", 2.0)
    m = tm.match("acme", V, "go", "claude-opus-4-8", "bob", 2.0)
    assert m.tier == "warm_start"
    assert m.same_language is False
    # warm starts book a fraction of the cost, not the whole thing
    assert 0 < m.shadow_usd < 2.0


def test_your_own_earlier_work_is_not_team_memory():
    tm = TeamMemory()
    tm.add("acme", V, "python", "claude-opus-4-8", "alice", 2.0)
    assert tm.match("acme", V, "python", "claude-opus-4-8", "alice", 2.0) is None


def test_an_unrelated_ask_does_not_match():
    tm = TeamMemory()
    tm.add("acme", V, "python", "claude-opus-4-8", "alice", 2.0)
    assert tm.match("acme", V_FAR, "python", "claude-opus-4-8", "bob", 2.0) is None


def test_a_different_tenant_never_matches():
    tm = TeamMemory()
    tm.add("acme", V, "python", "claude-opus-4-8", "alice", 2.0)
    assert tm.match("globex", V, "python", "claude-opus-4-8", "bob", 2.0) is None


def test_language_detection_picks_the_obvious_stack():
    assert detect_language("build an idempotent checkout in Go with goroutines") == "go"
    assert detect_language("write a pytest for the python parser") == "python"
    assert detect_language("") is None
