import json

from abletonosc.probe import describe_object, has_member, search_members


class FakeLiveObject:
    visible_value = 42
    _private_value = "secret"

    @property
    def broken_property(self):
        raise RuntimeError("cannot inspect")

    def freeze(self):
        return "mutating method not called"

    def add_name_listener(self, callback):
        pass


class OrderedObject:
    alpha = 1
    beta = 2
    gamma = 3
    _hidden = 4

    @property
    def explosive(self):
        raise AssertionError("names-only should not inspect values")

    def __dir__(self):
        return ["gamma", "_hidden", "explosive", "beta", "alpha"]


def test_describe_object_lists_public_members_without_calling_methods():
    result = describe_object(FakeLiveObject(), include_private=False)

    names = {member["name"]: member for member in result["members"]}
    assert result["class"].endswith("FakeLiveObject")
    assert "visible_value" in names
    assert names["visible_value"]["kind"] == "property"
    assert "freeze" in names
    assert names["freeze"]["kind"] == "callable"
    assert "broken_property" in names
    assert names["broken_property"]["kind"] == "error"
    assert "_private_value" not in names
    assert names["add_name_listener"]["listener_for"] == "name"


def test_describe_object_can_include_private_members():
    result = describe_object(FakeLiveObject(), include_private=True)
    names = {member["name"] for member in result["members"]}
    assert "_private_value" in names


def test_describe_object_supports_member_pagination():
    result = describe_object(
        OrderedObject(),
        include_private=False,
        max_members=2,
        offset=1,
    )

    assert result["names"] == ["beta", "explosive"]
    assert [member["name"] for member in result["members"]] == ["beta", "explosive"]
    assert result["offset"] == 1
    assert result["max_members"] == 2
    assert result["total_members"] == 4
    assert result["returned_members"] == 2
    assert result["next_offset"] == 3
    assert result["truncated"] is True


def test_describe_object_names_only_does_not_getattr_members():
    result = describe_object(
        OrderedObject(),
        include_private=False,
        names_only=True,
    )

    assert result["names"] == ["alpha", "beta", "explosive", "gamma"]
    assert result["members"] == [
        {"name": "alpha"},
        {"name": "beta"},
        {"name": "explosive"},
        {"name": "gamma"},
    ]


def test_search_members_filters_case_insensitively():
    result = search_members(FakeLiveObject(), "FREE")
    assert [member["name"] for member in result["matches"]] == ["freeze"]


def test_search_members_can_return_compact_names_only_matches():
    result = search_members(
        OrderedObject(),
        "a",
        max_members=2,
        names_only=True,
    )

    assert result["matches"] == [{"name": "alpha"}, {"name": "beta"}]
    assert result["total_matches"] == 3
    assert result["returned_matches"] == 2
    assert result["truncated"] is True


def test_has_member_reports_callable_status():
    result = has_member(FakeLiveObject(), "freeze")
    assert result["exists"] is True
    assert result["kind"] == "callable"


def test_has_member_reports_property_errors():
    result = has_member(FakeLiveObject(), "broken_property")
    assert result["exists"] is True
    assert result["kind"] == "error"
    assert "cannot inspect" in result["error"]


def test_results_are_json_serializable():
    payload = describe_object(FakeLiveObject(), include_private=False)
    encoded = json.dumps(payload)
    assert "FakeLiveObject" in encoded
