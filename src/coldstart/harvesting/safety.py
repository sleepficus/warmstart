"""Safety scanning for untrusted session content.

Two independent concerns:

- **Indirect prompt injection.** A transcript can contain text written to make
  a later agent do something. WarmStart's output is read by agents, so a
  harvester that copies transcript text into `AGENTS.md` is an injection
  delivery mechanism. Detection here is a tripwire, not a filter: a flagged
  event is quarantined for a human, never cleaned up and passed along.

- **Secrets and personal data.** Scanning can raise suspicion. It can never
  establish safety, and this module deliberately provides no function that
  returns "this content is safe" — the strongest available statement is
  "a scan ran and found these things", which is what `scan_secrets` returns.
"""

import re

from coldstart.domain import SafetyFinding, Sensitivity

# Patterns that indicate text is addressing a future agent rather than
# describing what happened. Ordered roughly by how unambiguous they are.
_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "instruction-override",
        re.compile(
            r"\b(ignore|disregard|forget)\b[^.\n]{0,40}\b"
            r"(previous|prior|earlier|above|all)\b[^.\n]{0,20}"
            r"\b(instruction|prompt|rule|direction|context)s?\b",
            re.IGNORECASE,
        ),
        "Text attempts to override earlier instructions.",
    ),
    (
        "role-assertion",
        re.compile(
            r"(^|\n)\s*(system|assistant|developer)\s*:\s*\S",
            re.IGNORECASE,
        ),
        "Text impersonates a system, assistant, or developer turn.",
    ),
    (
        "authority-claim",
        re.compile(
            r"\b(anthropic|openai|the (system|admin|administrator)|your (creator|developer))\b"
            r"[^.\n]{0,40}\b(require|instruct|authorize|permit|command)s?\b",
            re.IGNORECASE,
        ),
        "Text claims external authority over the agent.",
    ),
    (
        "directive-to-agent",
        re.compile(
            r"\byou (must|should|will|are required to|are instructed to)\b"
            r"[^.\n]{0,60}\b(write|publish|commit|push|delete|run|execute|send|ignore)\b",
            re.IGNORECASE,
        ),
        "Text directs an agent to take an action.",
    ),
    (
        "hidden-text",
        re.compile(r"<!--(?:(?!-->).){40,}-->", re.DOTALL),
        "Long HTML comment: content hidden from rendered views.",
    ),
    (
        "tool-directive",
        re.compile(
            r"<\s*/?\s*(system-reminder|important_instructions|antml:invoke)\b",
            re.IGNORECASE,
        ),
        "Text contains control markup that a harness might interpret.",
    ),
]

_SECRET_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "PEM private key block."),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "AWS access key id."),
    (
        "anthropic-key",
        re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}"),
        "Anthropic API key.",
    ),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"), "OpenAI-style API key."),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "GitHub token."),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "Slack token."),
    ("jwt", re.compile(r"\bey[A-Za-z0-9_-]{10,}\.ey[A-Za-z0-9_-]{10,}\."), "JSON web token."),
    (
        "bearer-header",
        re.compile(r"\b(authorization|bearer|api[_-]?key|secret)\b\s*[:=]\s*\S{8,}", re.IGNORECASE),
        "Credential-shaped assignment.",
    ),
    (
        "email-address",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        "Email address (personal data).",
    ),
]

# Findings at or above this rank force the event's classification upward.
_SECRET_ESCALATION = {
    "private-key": Sensitivity.SECRET,
    "aws-access-key": Sensitivity.SECRET,
    "anthropic-key": Sensitivity.SECRET,
    "openai-key": Sensitivity.SECRET,
    "github-token": Sensitivity.SECRET,
    "slack-token": Sensitivity.SECRET,
    "jwt": Sensitivity.SECRET,
    "bearer-header": Sensitivity.CONFIDENTIAL,
    "email-address": Sensitivity.CONFIDENTIAL,
}


def _hint(text: str, match: re.Match[str]) -> str:
    """Describe where a match occurred without reproducing what it matched.

    A finding that quoted the secret it found would copy that secret into a
    second file.
    """
    start = max(0, match.start() - 12)
    prefix = text[start : match.start()].replace("\n", " ")
    return f"…{prefix}[redacted {match.end() - match.start()} chars]…"


def scan_injection(text: str) -> list[SafetyFinding]:
    """Detect text that appears to address a future agent."""
    findings: list[SafetyFinding] = []
    for kind, pattern, detail in _INJECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            findings.append(
                SafetyFinding(kind=kind, detail=detail, excerpt_hint=_hint(text, match))
            )
    return findings


def scan_secrets(text: str) -> list[SafetyFinding]:
    """Report credential- and identity-shaped content.

    An empty result means this scan matched nothing. It does not mean the text
    is safe to distribute, and no caller may treat it that way.
    """
    findings: list[SafetyFinding] = []
    for kind, pattern, detail in _SECRET_PATTERNS:
        match = pattern.search(text)
        if match:
            findings.append(
                SafetyFinding(kind=kind, detail=detail, excerpt_hint=_hint(text, match))
            )
    return findings


def escalated_sensitivity(base: str, secret_findings: list[SafetyFinding]) -> str:
    """Raise a classification to account for what a scan found.

    Classification only ever moves upward. Nothing here can declassify.
    """
    from coldstart.domain import max_sensitivity

    levels = [base]
    for finding in secret_findings:
        escalation = _SECRET_ESCALATION.get(finding.kind)
        if escalation is not None:
            levels.append(escalation.value)
    return max_sensitivity(*levels)
