"""Hostile chat inputs must give a clean 4xx, never an HTTP 500 (a 500 also posts a Slack alert). Found by the abuse test on 2026-11-02."""


def _post(client, user, raw):
    return client.post("/v1/rehab/query", data=raw, content_type="application/json", headers=user["headers"])


def test_deeply_nested_json_is_a_clean_4xx(client, make_user):
    u = make_user(condition_codes=["arthritis_knee"])
    for raw in ("[" * 6000, '{"question": ' + "[" * 6000, '{"a":' * 3000):
        assert _post(client, u, raw).status_code in (400, 422)


def test_a_nul_byte_in_the_question_is_a_clean_422(client, make_user):
    u = make_user(condition_codes=["arthritis_knee"])
    r = _post(client, u, '{"question": "knee pain\\u0000 exercises"}')
    assert r.status_code == 422


def test_a_lone_surrogate_in_the_question_is_a_clean_422(client, make_user):
    u = make_user(condition_codes=["arthritis_knee"])
    r = _post(client, u, '{"question": "\\ud800 knee"}')
    assert r.status_code == 422


def test_normal_unicode_is_still_accepted(client, make_user):
    u = make_user(condition_codes=["arthritis_knee"])
    r = _post(client, u, '{"question": "फेवर 😀 fever"}')
    assert r.status_code == 200
