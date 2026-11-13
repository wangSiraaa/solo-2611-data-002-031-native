"""Unit tests for URL identity, link discovery and robots.txt handling."""
from __future__ import annotations

import pytest

from webfrontier.robots import parse_robots
from webfrontier.urls import (
    URLIdentityError,
    media_type,
    parse_html,
    parse_identity,
    resolve_link,
)


def test_fragment_is_removed():
    a = parse_identity("http://example.local:8080/a/b#frag")
    b = parse_identity("http://example.local:8080/a/b")
    assert a.canonical == b.canonical
    assert "#" not in a.canonical


def test_scheme_and_host_case_normalised():
    assert parse_identity("HTTP://Example.LOCAL/a").canonical == "http://example.local/a"
    assert parse_identity("HTTPS://EXAMPLE.LOCAL/a").canonical == "https://example.local/a"


def test_default_ports_removed():
    assert parse_identity("http://example.local:80/x").canonical == "http://example.local/x"
    assert parse_identity("https://example.local:443/x").canonical == "https://example.local/x"
    # non-default ports are preserved and are a different origin
    assert parse_identity("http://example.local:8080/x").canonical == "http://example.local:8080/x"


def test_different_ports_are_different_origins():
    a = parse_identity("http://127.0.0.1:8801/x")
    b = parse_identity("http://127.0.0.1:8802/x")
    assert a.origin != b.origin
    assert a.canonical != b.canonical


def test_dot_segments_removed_but_empty_segments_kept():
    assert parse_identity("http://h/a/./b/../c").canonical == "http://h/a/c"
    assert parse_identity("http://h/a//b").canonical == "http://h/a//b"
    assert parse_identity("http://h/../../x").path == "/x"


def test_query_order_and_duplicates_preserved_verbatim():
    u = parse_identity("http://h/p?b=2&a=1&a=1&a=3")
    assert u.query == "b=2&a=1&a=1&a=3"
    assert u.canonical.endswith("?b=2&a=1&a=1&a=3")
    # two different orderings are different identities, never sorted
    assert (
        parse_identity("http://h/p?a=1&b=2").canonical
        != parse_identity("http://h/p?b=2&a=1").canonical
    )


def test_empty_and_absent_query_are_distinct():
    assert parse_identity("http://h/p?").query == ""
    assert parse_identity("http://h/p").query is None
    assert parse_identity("http://h/p?").canonical == "http://h/p?"
    assert parse_identity("http://h/p").canonical == "http://h/p"


def test_encoded_slash_never_becomes_separator():
    a = parse_identity("http://h/a%2Fb")
    b = parse_identity("http://h/a/b")
    assert a.canonical != b.canonical
    assert a.path == "/a%2Fb"
    assert b.path == "/a/b"


def test_unreserved_escapes_normalised_and_escape_case_upper():
    assert parse_identity("http://h/%7Euser").canonical == "http://h/~user"
    assert parse_identity("http://h/a%2f").canonical == parse_identity("http://h/a%2F").canonical
    # %2F and / still stay distinct after normalisation
    assert parse_identity("http://h/a%2f").canonical != parse_identity("http://h/a/")


def test_invalid_urls_rejected():
    for bad in ["javascript:alert(1)", "mailto:a@b", "ftp://h/x", "http:///x", "http://h:99999/x"]:
        with pytest.raises(URLIdentityError):
            parse_identity(bad)
    with pytest.raises(URLIdentityError):
        parse_identity("http://h/a b")


def test_resolve_relative_and_absolute_links():
    assert resolve_link("http://h/item/detail", "../x?q=1#f") == "http://h/x?q=1"
    assert resolve_link("http://h/dir/", "child") == "http://h/dir/child"
    assert resolve_link("http://h/dir/", "//cdn/x") == "http://cdn/x"
    assert resolve_link("http://h/", "https://other/x") == "https://other/x"
    assert resolve_link("http://h/", "#top") is None
    assert resolve_link("http://h/", "javascript:void(0)") is None
    assert resolve_link("http://h/", "mailto:a@b") is None
    assert resolve_link("http://h/", "") is None


def test_first_valid_base_is_used_for_resolution():
    body = (
        b"<html><head><base href='/bogus'><base href='/item/'><title>T</title></head>"
        b"<body><a href='1001'>first</a><a href='second'>two</a></body></html>"
    )
    parsed = parse_html("http://h/list", body)
    # the first base wins even if we consider it odd; relative links resolve there
    assert parsed.base_url == "http://h/bogus"
    # "/bogus" has no trailing slash, so relative links replace the last segment
    assert [l.canonical for l in parsed.links] == [
        "http://h/1001",
        "http://h/second",
    ]


def test_invalid_base_is_skipped():
    body = (
        b"<html><head><base href='javascript:x'><base href='/item/'>"
        b"<title>T</title></head><body><a href='1001'>first</a></body></html>"
    )
    parsed = parse_html("http://h/list", body)
    assert parsed.base_url == "http://h/item/"
    assert parsed.links[0].canonical == "http://h/item/1001"


def test_links_keep_text_source_and_absolute_url():
    body = (
        b"<html><head><title>T</title></head><body>"
        b"<a href='x?a=1&a=1'>Link text</a><a href='/f/a.pdf'>Attachment</a>"
        b"</body></html>"
    )
    parsed = parse_html("http://h/dir/page", body)
    link = parsed.links[0]
    assert link.text == "Link text"
    assert link.canonical == "http://h/dir/x?a=1&a=1"
    assert link.base_url == "http://h/dir/page"
    assert parsed.attachments[0].canonical == "http://h/f/a.pdf"


def test_item_id_and_title_extracted():
    body = (
        b"<html><head><title>Item Title</title>"
        b"<meta name='item_id' content='ID-42'></head><body></body></html>"
    )
    parsed = parse_html("http://h/detail", body)
    assert parsed.title == "Item Title"
    assert parsed.item_id == "ID-42"


def test_broken_html_still_parses_or_raises_value_error():
    # selectolax is lenient; whatever it does, our contract is a ParsedHTML or ValueError
    parsed = parse_html("http://h/x", b"<html><head><title>broken")
    assert parsed.title == "broken"


def test_robots_specific_group_beats_wildcard():
    text = (
        "User-agent: *\nDisallow: /\n\n"
        "User-agent: CoopDirectoryBot\nDisallow: /private/\nAllow: /private/ok\n"
    )
    rules = parse_robots(text)
    assert rules.matched_group == "coopdirectorybot"
    assert rules.allowed(parse_identity("http://h/public/x"))
    assert not rules.allowed(parse_identity("http://h/private/x"))
    assert rules.allowed(parse_identity("http://h/private/ok"))


def test_robots_wildcard_group_when_no_specific_group():
    rules = parse_robots("User-agent: *\nDisallow: /secret/\n")
    assert rules.matched_group == "*"
    assert not rules.allowed(parse_identity("http://h/secret/a"))
    assert rules.allowed(parse_identity("http://h/public/a"))


def test_robots_no_groups_allows_everything():
    rules = parse_robots("# only comments\n\nSitemap: http://h/s.xml\n")
    assert rules.allowed(parse_identity("http://h/anything"))


def test_robots_multiple_groups_merged_case_insensitive():
    text = (
        "User-agent: CoopDirectoryBot\nDisallow: /one/\n\n"
        "user-agent: coopdirectorybot\nDisallow: /two/\n"
    )
    rules = parse_robots(text)
    assert not rules.allowed(parse_identity("http://h/one/x"))
    assert not rules.allowed(parse_identity("http://h/two/x"))
    assert rules.allowed(parse_identity("http://h/three/x"))


def test_robots_longest_match_and_allow_tiebreak():
    text = "User-agent: *\nDisallow: /page/\nAllow: /page/public\nDisallow: /a\nAllow: /a\n"
    rules = parse_robots(text)
    assert not rules.allowed(parse_identity("http://h/page/x"))
    assert rules.allowed(parse_identity("http://h/page/public/x"))
    assert rules.allowed(parse_identity("http://h/a"))  # equal length: Allow wins


def test_robots_wildcard_and_end_anchor():
    text = (
        "User-agent: *\n"
        "Disallow: /*.pdf$\n"
        "Disallow: /tmp/*/secret\n"
        "Disallow: /exact$\n"
    )
    rules = parse_robots(text)
    assert not rules.allowed(parse_identity("http://h/a/b/c.pdf"))
    assert rules.allowed(parse_identity("http://h/a/b/c.pdf?download=1"))
    assert not rules.allowed(parse_identity("http://h/tmp/x/secret"))
    assert not rules.allowed(parse_identity("http://h/tmp/x/y/secret"))
    assert not rules.allowed(parse_identity("http://h/exact"))
    assert rules.allowed(parse_identity("http://h/exact/more"))


def test_robots_query_string_participates_in_match():
    text = "User-agent: *\nDisallow: /search?q=\n"
    rules = parse_robots(text)
    assert not rules.allowed(parse_identity("http://h/search?q=x"))
    assert rules.allowed(parse_identity("http://h/search?other=1"))


def test_robots_rules_before_first_group_ignored():
    rules = parse_robots("Disallow: /\nUser-agent: *\nAllow: /\n")
    assert rules.allowed(parse_identity("http://h/x"))


def test_robots_empty_disallow_allows_all():
    rules = parse_robots("User-agent: *\nDisallow:\n")
    assert rules.allowed(parse_identity("http://h/x"))


def test_robots_percent_encoded_comparison():
    rules = parse_robots("User-agent: *\nDisallow: /caf%C3%A9\n")
    assert not rules.allowed(parse_identity("http://h/caf%C3%A9"))
    # reserved characters stay distinct: %2F is not /
    rules2 = parse_robots("User-agent: *\nDisallow: /a%2Fb\n")
    assert not rules2.allowed(parse_identity("http://h/a%2Fb"))
    assert rules2.allowed(parse_identity("http://h/a/b"))


def test_robots_txt_itself_implicitly_allowed():
    rules = parse_robots("User-agent: *\nDisallow: /\n")
    assert rules.allowed(parse_identity("http://h/robots.txt"))
    assert not rules.allowed(parse_identity("http://h/anything"))


def test_media_type_parsing():
    assert media_type("Text/HTML; charset=UTF-8") == "text/html"
    assert media_type(None) == ""
    assert media_type("application/octet-stream") == "application/octet-stream"
