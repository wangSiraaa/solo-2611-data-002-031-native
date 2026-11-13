"""robots.txt parsing and matching per RFC 9309.

Supported scope, stated explicitly:

* groups are selected by product token.  A group matches when its (case
  insensitive) token is a prefix of our USER_AGENT product token, which is the
  conventional interpretation of "the product token SHOULD be a substring"
  (RFC 9309 section 2.2.1).  All matching groups are merged; if none match, the
  ``*`` group, if present, is used; otherwise everything is allowed.
* rule matching uses the path + query of the identity, is case sensitive,
  starts at the first octet and takes the longest (most octets) match.  Equal
  length Allow beats Disallow.  ``*`` matches zero or more characters and a
  trailing ``$`` anchors the end of the match.  A ``*`` or ``$`` in the middle
  of a pattern is matched literally.
* percent escapes are compared after decoding unreserved octets, so
  ``%62%61%7A`` equals ``baz`` while ``%2F`` stays distinct from ``/``.
* 2xx => parse rules; 4xx => no rules (allow); 5xx / connect error => the site
  is unfetchable and must be temporarily skipped, never treated as allow.
* rules are frozen per origin for the lifetime of a task.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .urls import USER_AGENT, URLIdentity

_MAX_ROBOTS_BYTES = 500 * 1024  # RFC lower bound; we also cap reads here.


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str


@dataclass
class RobotsRules:
    rules: list[Rule] = field(default_factory=list)
    source: str = "none"  # parsed | absent | unfetchable | error
    http_status: int | None = None
    matched_group: str = ""

    def allowed(self, ident: URLIdentity) -> bool:
        # /robots.txt is implicitly allowed (RFC 9309 section 2.2.2).
        if ident.path == "/robots.txt":
            return True
        if self.source == "unfetchable":
            # An undefined robots.txt means complete disallow; callers normally
            # never see this object (the site is skipped), this is defensive.
            return False
        if self.source != "parsed":
            return True
        target = _matchable(ident.path_query)
        best: Rule | None = None
        best_len = -1
        for rule in self.rules:
            length = _match_length(rule.pattern, target)
            if length < 0:
                continue
            if length > best_len or (length == best_len and rule.allow):
                best = rule
                best_len = length
        return True if best is None else best.allow


def _matchable(text: str) -> str:
    """Decode percent escapes of unreserved octets only (RFC 9309 2.2.2)."""
    from .urls import _HEX, _UNRESERVED

    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "%" and i + 2 < len(text) and text[i + 1] in _HEX and text[i + 2] in _HEX:
            decoded = chr(int(text[i + 1 : i + 3], 16))
            out.append(decoded if decoded in _UNRESERVED else text[i : i + 3].upper())
            i += 3
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _match_length(pattern: str, target: str) -> int:
    """Return the number of target octets the pattern matches, or -1.

    A prefix rule (no ``$``) matches when the target starts with the pattern
    text.  A pattern ending in ``$`` must match the whole target.  ``*`` is a
    wildcard; only a literal ``*`` created by a percent-encoded ``%2A`` stays
    literal, since we compare the raw file text.
    """
    anchored = pattern.endswith("$") and not pattern.endswith("\\$")
    body = pattern[:-1] if anchored else pattern
    if "*" not in body:
        if anchored:
            return len(target) if target == body else -1
        return len(body) if target.startswith(body) else -1
    regex = "".join(
        ".*" if ch == "*" else re.escape(ch) for ch in body
    )
    regex = "^" + regex + ("$" if anchored else "")
    m = re.match(regex, target, re.DOTALL)
    if not m:
        return -1
    # Specificity is the number of literal octets in the match, which equals
    # the matched span minus the wildcard expansions.  The matched span is a
    # faithful and simple proxy: longer matched target => more specific.
    return m.end()


def parse_robots(text: str, product_token: str | None = None) -> RobotsRules:
    token = (product_token or _product_token(USER_AGENT)).lower()
    groups: list[tuple[list[str], list[Rule]]] = []
    current_agents: list[str] = []
    current_rules: list[Rule] = []
    seen_rule_in_group = False

    def flush() -> None:
        nonlocal current_agents, current_rules, seen_rule_in_group
        if current_agents:
            groups.append((current_agents, current_rules))
        current_agents = []
        current_rules = []
        seen_rule_in_group = False

    for raw_line in text.splitlines():
        line = raw_line
        if "#" in line:
            line = line.split("#", 1)[0]
        line = line.strip()
        if not line:
            continue
        if ":" not in line:
            # Unparseable line: RFC says try the rest, so ignore just this one.
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            if seen_rule_in_group:
                flush()
            if value:
                current_agents.append(value.lower())
        elif key in ("allow", "disallow"):
            if not current_agents:
                # Rules outside any group are ignored (RFC 9309 2.2.2).
                continue
            seen_rule_in_group = True
            if key == "allow":
                current_rules.append(Rule(True, value))
            elif value:  # an empty Disallow means allow everything
                current_rules.append(Rule(False, value))
        # Other records (Sitemap, Crawl-delay, ...) never terminate a group.
    flush()

    matched: list[Rule] = []
    matched_any = False
    matched_token = ""
    for agents, rules in groups:
        if any(token.startswith(agent) for agent in agents if agent != "*"):
            matched.extend(rules)
            matched_any = True
            matched_token = ",".join(agents)
    if not matched_any:
        for agents, rules in groups:
            if "*" in agents:
                matched.extend(rules)
                matched_any = True
                matched_token = "*"
    return RobotsRules(
        rules=matched,
        source="parsed",
        matched_group=matched_token,
    )


def _product_token(user_agent: str) -> str:
    first = user_agent.split()[0] if user_agent else "bot"
    return first.split("/", 1)[0]


def unfetchable_rules(status: int | None, source: str = "unfetchable") -> RobotsRules:
    return RobotsRules(rules=[], source=source, http_status=status)


def absent_rules(status: int | None) -> RobotsRules:
    return RobotsRules(rules=[], source="absent", http_status=status)


def rules_from_json(rules_json: str, matched_group: str = "", status: int | None = None) -> RobotsRules:
    """Rebuild frozen rules persisted by a previous run of the same task."""
    import json

    try:
        raw = json.loads(rules_json)
    except (TypeError, ValueError):
        raw = []
    rules = [
        Rule(bool(item["allow"]), str(item["pattern"]))
        for item in raw
        if isinstance(item, dict) and "allow" in item and "pattern" in item
    ]
    return RobotsRules(
        rules=rules, source="parsed", http_status=status, matched_group=matched_group
    )


ROBOTS_MAX_BYTES = _MAX_ROBOTS_BYTES
