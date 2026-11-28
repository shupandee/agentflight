"""Server-side redaction rules: a policy applied to spans before they are stored.

Pure Python with no server imports, so the same engine can later be used by the replayer.
The server loads a policy from a TOML file (``AGENTFLIGHT_REDACTION_FILE``) at startup and
applies it to every span from ``POST /v1/spans`` and ``POST /v1/traces`` before anything is
derived from the span (run name and metadata, event columns). See
``docs/design/server-side-redaction.md``.

A policy is a list of rules. Each rule has a ``name`` and one or more matchers:

* ``keys``: dict keys at any depth inside JSON values (payload attributes, run metadata,
  and plain string attributes that hold JSON), matched case-insensitively. In text that
  isn't parseable JSON (truncated payloads, broken JSON), ``"key": value`` pairs are
  matched in the text itself.
* ``patterns``: regular expressions (Python ``re``) replaced wherever they match a string.
* ``attributes``: attribute names (exact, or ``fnmatch`` globs such as ``otel.resource.*``)
  whose whole value is redacted.
* ``preset``: built-in patterns for key-shaped secrets: ``bearer``, ``aws-access-key-id``,
  ``openai-key``.

``action`` is ``mask`` (default: replace with the marker) or ``drop`` (remove the attribute
or dict entry; only for ``keys`` and ``attributes``). ``scope`` limits a rule to some of
``attributes``, ``events`` (span event attributes) and ``status`` (the status message).

Structural attributes (ids, kinds, names, counts, the replay and parent attributes) are
never redacted; see ``PROTECTED``.
"""

from __future__ import annotations

import fnmatch
import json
import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentflight import attributes as A

REDACTED = "agentflight.redacted"
"""Span attribute (JSON) the server writes: what it redacted, by which rules."""

FAILED = "[REDACTION_FAILED]"
"""Replaces a value whose redaction raised: an error never stores the original."""

DEFAULT_MARKER = "[REDACTED:{rule}]"
SCOPES = ("attributes", "events", "status")
ACTIONS = ("mask", "drop")
ON_ERROR = ("redact", "reject")

PROTECTED = frozenset(
    {
        A.KIND,
        A.RUN_NAME,
        A.RUN_DROPPED_SPANS,
        A.RUN_PARENT_RUN_ID,
        A.RUN_PARENT_SPAN_ID,
        A.RUN_PARENT_SOURCE,
        A.LLM_MODEL,
        A.LLM_TOKENS_IN,
        A.LLM_TOKENS_OUT,
        A.LLM_COST_USD,
        A.TOOL_NAME,
        A.NODE_NAME,
        A.NODE_STEP,
        A.DECISION_NAME,
        A.STATE_KEY,
        A.INTEGRATION,
        A.ERROR_TYPE,
        A.FAILURE_ID,
        A.TRUNCATED,
        REDACTED,
    }
)
"""Attributes no rule touches: ids, kinds, names (replay matches steps by them), counts,
cost, truncation and redaction info. Replay attributes (``agentflight.replay.*``) too."""

PRESETS: dict[str, tuple[str, ...]] = {
    # "Authorization: Bearer <token>" and bare "Bearer <token>"
    "bearer": (r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]{16,}=*",),
    # AWS access key ids (long-term AKIA, temporary ASIA)
    "aws-access-key-id": (r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",),
    # OpenAI-style secret keys (sk-..., sk-proj-...)
    "openai-key": (r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}",),
}

# Literal pre-check for the presets: every match of the pattern contains one of these
# fragments, so when none is in the text the regex is skipped. (fragments, ignore case):
# with ignore case the fragments are compared with the lowercased text, which is exact
# because no non-ASCII character matches "b", "e", "a" or "r" under (?i) (a test checks
# every code point).
_PREFILTERS: dict[str, tuple[tuple[str, ...], bool]] = {
    PRESETS["bearer"][0]: (("bearer",), True),
    PRESETS["aws-access-key-id"][0]: (("AKIA", "ASIA"), False),
    PRESETS["openai-key"][0]: (("sk-",), False),
}

_MAX_DEPTH = 32  # nested JSON walked; deeper values are treated as text
_JSON_START = ("{", "[")
# Attribute suffixes whose values the SDK writes as JSON text (see server.service).
_JSON_SUFFIXES = (
    ".metadata",
    ".messages",
    ".response",
    ".params",
    ".args",
    ".result",
    ".choice",
    ".old",
    ".new",
    ".input",
    ".output",
)
# Pattern features that look outside the matched text (anchors, lookarounds, conditionals):
# conservative, so a ``^`` inside a character class counts too.
_CONTEXT_FREE = re.compile(r"\^|\$|\\[AZ]|\(\?<?[=!]|\(\?\(")
# Nested quantifiers such as (a+)+ or (a*)* : the classic catastrophic-backtracking shape.
_NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*[+*}](?:[^()\\]|\\.)*\)\s*[+*{]")


class PolicyError(ValueError):
    """The redaction policy is invalid; the server refuses to start."""


def is_protected(key: str) -> bool:
    return key in PROTECTED or key.startswith("agentflight.replay.")


@dataclass(frozen=True)
class Rule:
    name: str
    keys: frozenset[str] = frozenset()
    patterns: tuple[re.Pattern[str], ...] = ()
    attributes: tuple[str, ...] = ()
    action: str = "mask"
    scope: frozenset[str] = frozenset(SCOPES)

    def matches_attribute(self, key: str) -> bool:
        return any(fnmatch.fnmatchcase(key, glob) for glob in self.attributes)


@dataclass
class Changes:
    """What one value's redaction did: rule names, how many replacements, dropped."""

    rules: list[str] = field(default_factory=list)
    count: int = 0
    dropped: bool = False
    failed: bool = False
    over_cap: bool = False

    def hit(self, rule: str, n: int = 1) -> None:
        if n <= 0:
            return
        if rule not in self.rules:
            self.rules.append(rule)
        self.count += n

    def info(self) -> dict[str, Any]:
        out: dict[str, Any] = {"rules": list(self.rules), "count": self.count}
        if self.dropped:
            out["dropped"] = True
        if self.over_cap:
            out["over_cap"] = True
        return out


@dataclass(frozen=True)
class Policy:
    rules: tuple[Rule, ...]
    marker: str = DEFAULT_MARKER
    on_error: str = "redact"
    # Text-level "key": value matching for each key-rule key, built once.
    # (rule, pattern, literal): literal is the lowercased key when it is ASCII (the pattern
    # then only matches its ASCII upper and lower case letters), else None (no pre-check).
    _text_keys: tuple[tuple[Rule, re.Pattern[str], str | None], ...] = ()
    _quoted: dict[str, str] = field(default_factory=dict, compare=False, repr=False)
    # Per scope: (key rule keys, patterns) for the quick "nothing can match" check.
    _quick: dict[str, tuple[tuple[str, ...], tuple[re.Pattern[str], ...], bool]] = field(
        default_factory=dict, compare=False, repr=False
    )

    # -- loading ---------------------------------------------------------------------

    @classmethod
    def from_file(cls, path: str | Path, *, on_error: str = "redact") -> Policy:
        try:
            with open(path, "rb") as fh:
                doc = tomllib.load(fh)
        except OSError as exc:
            raise PolicyError(f"can't read redaction policy {str(path)!r}: {exc}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise PolicyError(f"redaction policy {str(path)!r} isn't valid TOML: {exc}") from exc
        return cls.from_dict(doc, on_error=on_error)

    @classmethod
    def from_dict(cls, doc: dict[str, Any], *, on_error: str = "redact") -> Policy:
        if on_error not in ON_ERROR:
            raise PolicyError(f"on_error must be one of {ON_ERROR}, not {on_error!r}")
        unknown = set(doc) - {"version", "marker", "rules"}
        if unknown:
            raise PolicyError(f"unknown policy settings: {sorted(unknown)}")
        if doc.get("version", 1) != 1:
            raise PolicyError(f"unsupported policy version {doc.get('version')!r}")
        marker = doc.get("marker", DEFAULT_MARKER)
        if not isinstance(marker, str) or not marker:
            raise PolicyError("marker must be a non-empty string")
        raw_rules = doc.get("rules", [])
        if not isinstance(raw_rules, list) or not raw_rules:
            raise PolicyError("a policy needs at least one [[rules]] entry")
        rules = tuple(_parse_rule(r, i) for i, r in enumerate(raw_rules))
        names = [r.name for r in rules]
        if len(set(names)) != len(names):
            raise PolicyError("rule names must be unique")
        text_keys = tuple(
            (rule, _text_key_pattern(key), key if key.isascii() else None)
            for rule in rules
            if rule.keys
            for key in sorted(rule.keys)
        )
        quick = {
            scope: (
                tuple(sorted({k for r in rules if scope in r.scope for k in r.keys})),
                tuple(p for r in rules if scope in r.scope for p in r.patterns),
                all(
                    _CONTEXT_FREE.search(p.pattern) is None
                    for r in rules
                    if scope in r.scope
                    for p in r.patterns
                ),
            )
            for scope in SCOPES
        }
        policy = cls(
            rules=rules,
            marker=marker,
            on_error=on_error,
            _text_keys=text_keys,
            _quick=quick,
            _quoted={
                r.name: json.dumps(marker.replace("{rule}", r.name), ensure_ascii=False)
                for r in rules
            },
        )
        policy._check_markers()
        return policy

    def _check_markers(self) -> None:
        """Markers must not match any rule, so redacting twice changes nothing."""
        for rule in self.rules:
            for text in (self.marker_for(rule.name), FAILED):
                if any(p.search(text) for p in rule.patterns):
                    raise PolicyError(
                        f"rule {rule.name!r}: a pattern matches the marker {text!r}, so "
                        "redaction wouldn't be idempotent"
                    )

    def marker_for(self, rule: str) -> str:
        return self.marker.replace("{rule}", rule)

    # -- applying --------------------------------------------------------------------

    def _rules(self, scope: str) -> list[Rule]:
        return [r for r in self.rules if scope in r.scope]

    def redact_text(self, text: str, scope: str, changes: Changes) -> str:
        """Pattern rules, then text-level ``"key": value`` matching for key rules."""
        lowered: str | None = None
        for rule in self._rules(scope):
            marker = self.marker_for(rule.name)
            for pattern in rule.patterns:
                pre = _PREFILTERS.get(pattern.pattern)
                if pre is not None:
                    fragments, ignore_case = pre
                    if ignore_case and lowered is None:
                        lowered = text.lower()
                    hay = lowered if ignore_case else text
                    assert hay is not None
                    if not any(f in hay for f in fragments):
                        continue  # no match possible: skip the regex
                text, n = pattern.subn(marker, text)
                if n:
                    lowered = None
                changes.hit(rule.name, n)
        for rule, pattern, literal in self._text_keys:
            if scope not in rule.scope:
                continue
            if literal is not None:
                if lowered is None:
                    lowered = text.lower()
                if literal not in lowered:
                    continue  # the key isn't in the text: skip the regex
            quoted = self._quoted[rule.name]
            hits = 0

            def sub(m: re.Match[str], quoted: str = quoted) -> str:
                nonlocal hits
                if m.group(2) == quoted:
                    return m.group(0)  # already redacted
                hits += 1
                return m.group(1) + quoted

            text = pattern.sub(sub, text)
            if hits:
                lowered = None
            changes.hit(rule.name, hits)
        return text

    def _key_rule(self, key: str, scope: str) -> Rule | None:
        lowered = key.lower()
        for rule in self._rules(scope):
            if lowered in rule.keys:
                return rule
        return None

    def redact_json(self, value: Any, scope: str, changes: Changes, depth: int = 0) -> Any:
        """Walk a parsed JSON value: key rules on dict entries, strings as text."""
        if depth > _MAX_DEPTH:
            return value
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for k, v in value.items():
                rule = self._key_rule(str(k), scope)
                if rule is None:
                    out[k] = self.redact_json(v, scope, changes, depth + 1)
                elif v == self.marker_for(rule.name):
                    out[k] = v  # already redacted
                elif rule.action == "drop":
                    changes.hit(rule.name)
                else:
                    out[k] = self.marker_for(rule.name)
                    changes.hit(rule.name)
            return out
        if isinstance(value, list):
            return [self.redact_json(v, scope, changes, depth + 1) for v in value]
        if isinstance(value, str):
            return self.redact_string(value, scope, changes, depth + 1, json_hint=False)
        return value

    def _nothing_can_match(self, text: str, scope: str) -> bool:
        """True only if parsing and walking ``text`` would leave it unchanged.

        A cheap check that skips the JSON parse and walk for the common case of a payload
        that holds nothing for the policy. It is deliberately conservative; whenever it
        isn't sure it says False and the full walk runs.

        Without escapes, every string leaf and dict key of a JSON text is a substring of
        it, so a pattern that matches a leaf also matches the text, and a key that matches
        (lowercased, as the walk compares keys) appears in the lowercased text. The same
        holds for text mode, which sees the raw text when the value doesn't parse. It
        says False when:

        * the text has a backslash: an escape (``\\n``, ``\\"``, ``\\u0061``) makes a
          decoded leaf differ from the raw text, so a key or pattern could match only after
          parsing (``\\s`` against a decoded newline, ``\\u0061pi_key``);
        * a key rule's key appears in the lowercased text, or a pattern matches it;
        * a key rule has a non-ASCII key (lowercasing isn't letter by letter for a few
          non-ASCII letters, and text mode matches each letter's upper and lower case);
        * the scope has a pattern with an anchor or lookaround, which can match a string on
          its own but not inside the surrounding JSON text (``\\b`` is fine: the
          characters around a JSON string are quotes).
        """
        if "\\" in text:
            return False
        keys, patterns, context_free = self._quick[scope]
        if patterns and not context_free:
            return False
        lowered = text.lower()
        if keys:
            if not all(k.isascii() for k in keys):
                return False
            if any(k in lowered for k in keys):
                return False
        return not any(_may_match(p, text, lowered) and p.search(text) for p in patterns)

    def redact_string(
        self, text: str, scope: str, changes: Changes, depth: int = 0, *, json_hint: bool
    ) -> str:
        """A string value: parsed and walked if it is JSON (always tried when ``json_hint``,
        else only when it looks like an object or array), otherwise treated as text."""
        stripped = text.lstrip()
        if (json_hint or stripped.startswith(_JSON_START)) and depth <= _MAX_DEPTH:
            if self._nothing_can_match(text, scope):
                return text
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = _NOT_JSON
            if parsed is not _NOT_JSON and (json_hint or isinstance(parsed, (dict, list))):
                before = changes.count
                result = self.redact_json(parsed, scope, changes, depth)
                if changes.count == before:
                    return text  # unchanged: keep the original bytes
                return json.dumps(result, ensure_ascii=False)
        return self.redact_text(text, scope, changes)

    def redact_value(
        self, key: str, value: Any, scope: str, *, truncated: bool, max_chars: int | None
    ) -> tuple[Any, Changes]:
        """Redact one attribute value. Returns the new value (``_DROP`` to remove it)."""
        changes = Changes()
        for rule in self._rules(scope):
            if rule.attributes and rule.matches_attribute(key):
                changes.hit(rule.name)
                if rule.action == "drop":
                    changes.dropped = True
                    return _DROP, changes
                return self.marker_for(rule.name), changes
        if isinstance(value, str):
            if truncated:
                # A cut payload is no longer valid JSON: text-level matching only. A secret
                # cut in half by the truncation may not match.
                out: Any = self.redact_text(value, scope, changes)
            else:
                out = self.redact_string(
                    value, scope, changes, json_hint=key.endswith(_JSON_SUFFIXES)
                )
        elif isinstance(value, (dict, list)):
            out = self.redact_json(value, scope, changes)
        else:
            return value, changes
        if (
            changes.count
            and max_chars is not None
            and isinstance(out, str)
            and len(out) > max_chars
        ):
            # Markers can be longer than what they replace. Over the attribute cap, the
            # whole value becomes one marker (never cut: a cut could split a marker).
            out = self.marker_for(changes.rules[0])
            changes.over_cap = True
        return out, changes


class _NotJson:
    pass


_NOT_JSON: Any = _NotJson()
_DROP: Any = _NotJson()


def _may_match(pattern: re.Pattern[str], text: str, lowered: str) -> bool:
    """False when the literal pre-check rules a match of ``pattern`` out."""
    pre = _PREFILTERS.get(pattern.pattern)
    if pre is None:
        return True
    fragments, ignore_case = pre
    hay = lowered if ignore_case else text
    return any(f in hay for f in fragments)


def _text_key_pattern(key: str) -> re.Pattern[str]:
    """``"key": value`` in JSON-ish text, value a string (possibly cut off), number,
    true, false or null. Group 1 is everything up to the value, group 2 the value."""
    name = "".join(f"[{c.lower()}{c.upper()}]" if c.isalpha() else re.escape(c) for c in key)
    return re.compile(
        r'("' + name + r'"\s*:\s*)'
        r'("(?:[^"\\]|\\.)*(?:"|\\?$)|-?\d[\d.eE+\-]*|true|false|null)'
    )


def _strings(value: Any, field_name: str, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise PolicyError(f"{where}: {field_name} must be a list of non-empty strings")
    return list(value)


def _parse_rule(raw: Any, index: int) -> Rule:
    where = f"rule #{index + 1}"
    if not isinstance(raw, dict):
        raise PolicyError(f"{where}: must be a table")
    allowed = {"name", "keys", "patterns", "attributes", "preset", "action", "scope"}
    unknown = set(raw) - allowed
    if unknown:
        raise PolicyError(f"{where}: unknown settings {sorted(unknown)}")
    name = raw.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", name):
        raise PolicyError(f"{where}: name must be 1-64 letters, digits, '_', '.' or '-'")
    where = f"rule {name!r}"
    keys = _strings(raw.get("keys"), "keys", where)
    pattern_texts = _strings(raw.get("patterns"), "patterns", where)
    for preset in _strings(raw.get("preset"), "preset", where):
        if preset not in PRESETS:
            raise PolicyError(f"{where}: unknown preset {preset!r} (known: {sorted(PRESETS)})")
        pattern_texts.extend(PRESETS[preset])
    attrs = _strings(raw.get("attributes"), "attributes", where)
    for attr in attrs:
        if not any(ch in attr for ch in "*?[") and is_protected(attr):
            raise PolicyError(f"{where}: {attr!r} is a structural attribute and is never redacted")
    action = raw.get("action", "mask")
    if action not in ACTIONS:
        raise PolicyError(f"{where}: action must be one of {ACTIONS}")
    if action == "drop" and pattern_texts:
        raise PolicyError(f"{where}: action 'drop' works with keys and attributes, not patterns")
    scope = _strings(raw.get("scope"), "scope", where) or list(SCOPES)
    bad = set(scope) - set(SCOPES)
    if bad:
        raise PolicyError(f"{where}: unknown scope {sorted(bad)} (known: {list(SCOPES)})")
    if not (keys or pattern_texts or attrs):
        raise PolicyError(f"{where}: needs keys, patterns, attributes or a preset")
    patterns = []
    for text in pattern_texts:
        if _NESTED_QUANTIFIER.search(text):
            raise PolicyError(
                f"{where}: pattern {text!r} has a nested quantifier, which can backtrack "
                "catastrophically (best-effort check)"
            )
        try:
            patterns.append(re.compile(text))
        except re.error as exc:
            raise PolicyError(f"{where}: invalid pattern {text!r}: {exc}") from exc
    return Rule(
        name=name,
        keys=frozenset(k.lower() for k in keys),
        patterns=tuple(patterns),
        attributes=tuple(attrs),
        action=action,
        scope=frozenset(scope),
    )


# -- spans -------------------------------------------------------------------------------


@dataclass
class SpanResult:
    """The redacted parts of one span, or ``error`` if it must be rejected."""

    attributes: dict[str, Any]
    events: list[dict[str, Any]]
    status_message: str | None
    error: str | None = None


def _truncated_keys(attributes: dict[str, Any]) -> set[str]:
    raw = attributes.get(A.TRUNCATED)
    try:
        doc = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return set()
    return set(doc) if isinstance(doc, dict) else set()


def redact_span_parts(
    policy: Policy,
    attributes: dict[str, Any],
    events: Iterable[tuple[str, int, dict[str, Any]]],
    status_message: str | None,
    *,
    max_chars: int | None,
) -> SpanResult:
    """Apply ``policy`` to a span's attributes, span events (name, time, attributes) and
    status message. Never raises.

    A value whose redaction raises becomes ``FAILED`` (``on_error="redact"``) or makes the
    span rejected (``on_error="reject"``). If the whole span can't be processed, every
    value that could hold data (strings, structures and the status message) becomes
    ``FAILED`` (or the span is rejected). ``agentflight.redacted`` is always the server's
    own: a sender's value is replaced (or removed when nothing was redacted).
    """
    try:
        return _redact_span(policy, attributes, list(events), status_message, max_chars)
    except Exception:
        if policy.on_error == "reject":
            return SpanResult({}, [], None, error="redaction failed")
        return _all_failed(attributes, list(events), status_message)


def _all_failed(
    attributes: dict[str, Any],
    events: list[tuple[str, int, dict[str, Any]]],
    status_message: str | None,
) -> SpanResult:
    def wipe(attrs: dict[str, Any], prefix: str, report: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in attrs.items():
            if is_protected(k) or not isinstance(v, (str, dict, list)):
                out[k] = v
            else:
                out[k] = FAILED
                report[prefix + k] = {"rules": ["<error>"], "count": 1}
        return out

    report: dict[str, Any] = {}
    attrs = wipe({k: v for k, v in attributes.items() if k != REDACTED}, "", report)
    new_events = [
        {"name": name, "time_ns": t, "attributes": wipe(ea, f"events[{i}].", report)}
        for i, (name, t, ea) in enumerate(events)
    ]
    message = None
    if status_message is not None:
        message = FAILED
        report["status_message"] = {"rules": ["<error>"], "count": 1}
    attrs[REDACTED] = json.dumps(report, separators=(",", ":"))
    return SpanResult(attrs, new_events, message)


def _redact_span(
    policy: Policy,
    attributes: dict[str, Any],
    events: list[tuple[str, int, dict[str, Any]]],
    status_message: str | None,
    max_chars: int | None,
) -> SpanResult:
    report: dict[str, Any] = {}
    truncated = _truncated_keys(attributes)
    failure: list[str] = []

    def one(key: str, value: Any, scope: str, label: str, is_truncated: bool) -> Any:
        try:
            new, changes = policy.redact_value(
                key, value, scope, truncated=is_truncated, max_chars=max_chars
            )
        except Exception:
            failure.append(label)
            report[label] = {"rules": ["<error>"], "count": 1}
            return FAILED
        if changes.count:
            report[label] = changes.info()
        return new

    def attrs_of(source: dict[str, Any], scope: str, prefix: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in source.items():
            if key == REDACTED and not prefix:
                continue  # the server's own, recomputed below
            if is_protected(key):
                out[key] = value
                continue
            new = one(key, value, scope, prefix + key, not prefix and key in truncated)
            if new is not _DROP:
                out[key] = new
        return out

    new_attrs = attrs_of(attributes, "attributes", "")
    new_events = [
        {"name": name, "time_ns": t, "attributes": attrs_of(ea, "events", f"events[{i}].")}
        for i, (name, t, ea) in enumerate(events)
    ]
    message = status_message
    if status_message is not None:
        changes = Changes()
        try:
            message = policy.redact_text(status_message, "status", changes)
        except Exception:
            failure.append("status_message")
            changes = Changes(rules=["<error>"], count=1)
            message = FAILED
        if changes.count:
            report["status_message"] = changes.info()
    if failure and policy.on_error == "reject":
        return SpanResult({}, [], None, error=f"redaction failed for {', '.join(failure[:5])}")
    if report:
        new_attrs[REDACTED] = json.dumps(report, separators=(",", ":"))
    return SpanResult(new_attrs, new_events, message)
