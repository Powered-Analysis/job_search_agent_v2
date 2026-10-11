import socket

import pytest

from jsa.naming import company_from_board, normalize_company, title_slug

HOSTILE = '/\\:*?"<>|'


def test_company_suffix_stripped():
    assert normalize_company("Acme Widgets, Inc.") == "Acme Widgets"


@pytest.mark.parametrize(
    "company, expected",
    [
        ("acme widgets llc", "acme widgets"),
        ("ACME WIDGETS", "ACME WIDGETS"),
        ("Acme Widgets Inc", "Acme Widgets"),
        ("Acme Widgets Ltd.", "Acme Widgets"),
    ],
)
def test_company_suffixes_stripped_and_case_kept(company, expected):
    assert normalize_company(company) == expected


@pytest.mark.parametrize(
    "company",
    [
        "Acme/Widgets",
        "A\\B",
        "Foo: Bar*",
        'What? "Co" <x>',
        "Pipe|Co, Inc.",
        'a/b\\c:d*e?f"g<h>i|j',
    ],
)
def test_company_has_no_path_hostile_characters(company):
    out = normalize_company(company)
    assert out
    assert not set(out) & set(HOSTILE)


@pytest.mark.parametrize(
    "title",
    [
        "Senior Engineer",
        "Senior Engineer / Platform: Backend",
        'Staff "Data" Engineer <Remote> | US?',
        "C:\\Users\\evil*",
        "../../etc/passwd",
    ],
)
def test_title_slug_path_safe(title):
    slug = title_slug(title)
    assert slug
    assert not set(slug) & set(HOSTILE)
    assert len(slug) <= 80


@pytest.mark.parametrize("length", [79, 80, 81, 200, 1000])
def test_title_slug_at_most_80_characters(length):
    assert len(title_slug("Engineer " * (length // 9 + 1))[:]) <= 80
    assert len(title_slug("x" * length)) <= 80
    assert (
        len(title_slug("Senior Software Engineer, Platform Infrastructure " * 20)) <= 80
    )


def test_title_slug_deterministic():
    assert title_slug("Senior Engineer / Platform") == title_slug(
        "Senior Engineer / Platform"
    )


def test_title_slug_distinguishes_different_titles():
    assert title_slug("Backend Engineer") != title_slug("Frontend Engineer")


def test_naming_pure_no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("naming touched the network")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    assert normalize_company("Acme Widgets, Inc.") == "Acme Widgets"
    assert title_slug("Senior Engineer")


@pytest.mark.parametrize(
    "company",
    [
        "Acme & Co",
        "Acme and Co.",
        "Acme AND CO",
        "Acme, and Co",
        "Acme & Co, Inc.",
        "Acme and Co Ltd",
        "Acme & Inc",
        "  Acme & Co  ",
    ],
)
def test_company_connector_trimmed_with_suffix(company):
    assert normalize_company(company) == "Acme"


def test_company_connector_trimmed_with_suffix_case_kept():
    assert normalize_company("acme & co.") == "acme"


@pytest.mark.parametrize(
    "company, expected",
    [
        ("Black & Decker", "Black & Decker"),
        ("Johnson and Johnson", "Johnson and Johnson"),
        ("Procter & Gamble", "Procter & Gamble"),
        ("Fish and Chips Inc", "Fish and Chips"),
        ("Acme Widgets, Inc.", "Acme Widgets"),
        ("Brand Co", "Brand"),
        ("Sand Inc", "Sand"),
        ("Acme Widgets & Gadgets LLC", "Acme Widgets & Gadgets"),
    ],
)
def test_company_connector_kept_when_no_suffix_follows(company, expected):
    assert normalize_company(company) == expected


def test_company_connector_without_suffix_is_left_alone():
    assert normalize_company("Acme &") == "Acme &"


@pytest.mark.parametrize(
    "company, expected",
    [
        ("EliseAI", "EliseAI"),
        ("Far AI, Inc.", "Far AI"),
        ("acme corp", "acme"),
        ("iRobot Corporation", "iRobot"),
        ("  eBay   Inc  ", "eBay"),
        ("McKinsey/QuantumBlack LLC", "McKinseyQuantumBlack"),
    ],
)
def test_company_keeps_the_capitalization_it_was_given(company, expected):
    assert normalize_company(company) == expected


@pytest.mark.parametrize("company", ["Inc", "inc.", "LLC", "Corp"])
def test_company_named_only_a_suffix_keeps_its_name(company):
    assert normalize_company(company) == company.strip(" .")


def test_company_differing_only_in_case_stays_different():
    assert normalize_company("EliseAI") != normalize_company("Eliseai")


@pytest.mark.parametrize(
    "board, expected",
    [
        ("acme-corp", "Acme Corp"),
        ("eliseai", "Eliseai"),
        ("far_ai", "Far Ai"),
        ("ACME", "Acme"),
    ],
)
def test_company_from_board_is_still_title_cased(board, expected):
    assert company_from_board(board) == expected
