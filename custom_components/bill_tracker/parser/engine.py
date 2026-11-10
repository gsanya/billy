"""Declarative YAML parser engine used by Billy 0.6.0."""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import DocumentBundle, MailEnvelope

MAX_TEXT_SIZE = 1_500_000
MAX_PATTERN_SIZE = 600

_MONTHS_IT = {
    "gennaio": 1,
    "gen": 1,
    "febbraio": 2,
    "feb": 2,
    "marzo": 3,
    "mar": 3,
    "aprile": 4,
    "apr": 4,
    "maggio": 5,
    "mag": 5,
    "giugno": 6,
    "giu": 6,
    "luglio": 7,
    "lug": 7,
    "agosto": 8,
    "ago": 8,
    "settembre": 9,
    "set": 9,
    "ottobre": 10,
    "ott": 10,
    "novembre": 11,
    "nov": 11,
    "dicembre": 12,
    "dic": 12,
}


class ParserError(ValueError):
    """Raised when a parser cannot safely parse the supplied message."""


class ParserEngine:
    """Evaluate metadata matching and extract normalized bill fields."""

    def prefilter(self, parser: dict[str, Any], envelope: MailEnvelope) -> bool:
        config = parser.get("prefilter", {}).get("email", {})
        senders = [str(item).strip().casefold() for item in config.get("from", []) if str(item).strip()]
        if senders:
            sender = self._email_address(envelope.sender).casefold()
            if sender not in senders:
                return False

        subject = envelope.subject.casefold()
        contains = [str(item).strip().casefold() for item in config.get("subject_contains", []) if str(item).strip()]
        if contains and not any(item in subject for item in contains):
            return False

        regexes = config.get("subject_regex", []) or []
        if regexes and not any(self._search(str(pattern), envelope.subject) for pattern in regexes):
            return False
        return True

    def detect(
        self,
        parser: dict[str, Any],
        envelope: MailEnvelope,
        documents: DocumentBundle | None = None,
    ) -> tuple[bool, int, int]:
        detection = parser.get("detection", {})
        threshold = int(detection.get("threshold", 1))
        score = 0
        for rule in detection.get("rules", []):
            if self._rule_matches(rule, envelope, documents):
                score += int(rule.get("weight", 0))
        return score >= threshold, score, threshold

    def parse(
        self,
        parser: dict[str, Any],
        envelope: MailEnvelope,
        documents: DocumentBundle,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        provenance: dict[str, dict[str, Any]] = {}

        for field_name, field in parser.get("fields", {}).items():
            if "value" in field:
                result[field_name] = field.get("value")
                provenance[field_name] = {"source": "static", "raw": field.get("value")}
                continue

            matched = self._extract_field(field, documents)
            if matched is None:
                if field.get("required", False):
                    raise ParserError(f"Required field '{field_name}' was not found")
                continue

            raw_value, match, candidate = matched
            transform = field.get("transform") or {"type": "text"}
            transformed = self._transform(raw_value, match, transform)
            outputs = field.get("outputs") or []
            source_name = str(candidate.get("source", ""))

            if isinstance(transformed, dict):
                for output_name, value in transformed.items():
                    if outputs and output_name not in outputs:
                        continue
                    result[output_name] = value
                    provenance[output_name] = {"source": source_name, "raw": raw_value}
            else:
                result[field_name] = transformed
                provenance[field_name] = {"source": source_name, "raw": raw_value}

        self._validate_result(result)
        return {"data": result, "provenance": provenance}

    def verification(
        self,
        parser: dict[str, Any],
        documents: DocumentBundle,
    ) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        fields = parser.get("fields", {})
        for config in parser.get("verification", []) or []:
            field_name = str(config.get("field", ""))
            field = fields.get(field_name)
            if not field or "candidates" not in field:
                continue
            values: dict[str, Any] = {}
            for source in config.get("sources", []):
                source = str(source)
                field_for_source = dict(field)
                field_for_source["candidates"] = [
                    candidate
                    for candidate in field.get("candidates", [])
                    if str(candidate.get("source")) == source
                ]
                if not field_for_source["candidates"]:
                    continue
                matched = self._extract_field(field_for_source, documents)
                if matched is None:
                    continue
                raw_value, match, _candidate = matched
                try:
                    transformed = self._transform(
                        raw_value,
                        match,
                        field.get("transform") or {"type": "text"},
                    )
                except ParserError:
                    continue
                if not isinstance(transformed, dict):
                    values[source] = transformed
            if len(values) < 2:
                continue
            normalized = [self._compare_value(value) for value in values.values()]
            checks.append(
                {
                    "field": field_name,
                    "sources": values,
                    "match": len(set(normalized)) == 1,
                }
            )
        return checks

    def _extract_field(
        self, field: dict[str, Any], documents: DocumentBundle
    ) -> tuple[str, re.Match[str], dict[str, Any]] | None:
        for candidate in field.get("candidates", []) or []:
            source_name = str(candidate.get("source", ""))
            source = self._bounded(documents.source(source_name))
            if not source:
                continue
            match = self._search(str(candidate.get("regex", "")), source)
            if match is None:
                continue
            group_name = str(candidate.get("group") or "value")
            if group_name in match.groupdict():
                raw = match.group(group_name)
            elif match.lastindex:
                raw = match.group(1)
            else:
                raw = match.group(0)
            return str(raw).strip(), match, candidate
        return None

    def _rule_matches(
        self,
        rule: dict[str, Any],
        envelope: MailEnvelope,
        documents: DocumentBundle | None,
    ) -> bool:
        values = self._rule_values(str(rule.get("source", "")), envelope, documents)
        if not values:
            return False
        for raw in values:
            value = str(raw)
            if "equals" in rule and value.strip().casefold() == str(rule["equals"]).strip().casefold():
                return True
            if "contains" in rule and str(rule["contains"]).casefold() in value.casefold():
                return True
            if "regex" in rule and self._search(str(rule["regex"]), self._bounded(value)):
                return True
        return False

    def _rule_values(
        self,
        source: str,
        envelope: MailEnvelope,
        documents: DocumentBundle | None,
    ) -> list[str]:
        if source == "email.from":
            return [self._email_address(envelope.sender)]
        if source == "email.subject":
            return [envelope.subject]
        if source == "email.body":
            return [documents.email] if documents else []
        if source == "attachment.filename":
            return [part.filename for part in envelope.parts if part.filename]
        if source == "attachment.mime_type":
            return [part.content_type for part in envelope.parts if part.content_type]
        return []

    def _transform(
        self,
        raw: str,
        match: re.Match[str],
        transform: dict[str, Any],
    ) -> Any:
        kind = str(transform.get("type", "text"))
        if kind == "text":
            return self._clean_text(raw)
        if kind == "decimal":
            return self._decimal(raw, str(transform.get("locale", "")))
        if kind == "date":
            return self._date(raw, str(transform.get("locale", "")), transform.get("formats"))
        if kind == "date_range":
            start_group = str(transform.get("start_group", "start"))
            end_group = str(transform.get("end_group", "end"))
            groups = match.groupdict()
            if start_group not in groups or end_group not in groups:
                raise ParserError("date_range requires named start and end groups")
            end_date = self._date(groups[end_group], str(transform.get("locale", "")), None)
            start_raw = groups[start_group]
            start_date = self._date(
                start_raw,
                str(transform.get("locale", "")),
                None,
                fallback_year=(int(end_date[:4]) if transform.get("infer_missing_year", False) else None),
            )
            return {"period_start": start_date, "period_end": end_date}
        raise ParserError(f"Unsupported transform '{kind}'")

    def _date(
        self,
        raw: str,
        locale: str,
        formats: list[str] | None,
        fallback_year: int | None = None,
    ) -> str:
        text = self._clean_text(raw)
        for fmt in formats or []:
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                pass

        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y"):
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                pass

        if locale.lower().startswith("it"):
            normalized = self._strip_accents(text.casefold())
            month_map = {self._strip_accents(key): value for key, value in _MONTHS_IT.items()}
            match = re.fullmatch(r"(\d{1,2})\s+([a-z]+)(?:\s+(\d{2}|\d{4}))?", normalized)
            if match:
                day = int(match.group(1))
                month = month_map.get(match.group(2))
                year_text = match.group(3)
                year = int(year_text) if year_text else fallback_year
                if year_text and len(year_text) == 2:
                    year += 2000
                if month and year:
                    try:
                        return date(year, month, day).isoformat()
                    except ValueError as err:
                        raise ParserError(f"Invalid date '{raw}'") from err
        raise ParserError(f"Unsupported date '{raw}'")

    def _decimal(self, raw: str, locale: str) -> float:
        text = raw.strip().replace("\u00a0", "").replace(" ", "").replace("\u2212", "-")
        if text.endswith("-") and not text.startswith("-"):
            text = "-" + text[:-1]  # trailing minus, e.g. "1234-"
        if locale.lower().startswith(("it", "de", "es", "fr", "pt")):
            if "," in text:
                text = text.replace(".", "").replace(",", ".")
        elif text.count(",") == 1 and "." not in text:
            text = text.replace(",", ".")
        text = re.sub(r"[^0-9+\-.]", "", text)
        try:
            value = Decimal(text)
        except InvalidOperation as err:
            raise ParserError(f"Invalid decimal '{raw}'") from err
        return float(value)

    def _validate_result(self, result: dict[str, Any]) -> None:
        amount = result.get("amount")
        if amount is not None:
            value = float(amount)
            if not -1_000_000 <= value <= 1_000_000:
                raise ParserError("Amount outside the allowed range")
        consumption = result.get("consumption")
        if consumption is not None and float(consumption) < 0:
            raise ParserError("Consumption cannot be negative")
        start = result.get("period_start")
        end = result.get("period_end")
        if start and end and str(start) > str(end):
            raise ParserError("Billing period start is after period end")

    @staticmethod
    def _clean_text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @staticmethod
    def _compare_value(value: Any) -> str:
        if isinstance(value, float):
            return f"{value:.6f}"
        return str(value).strip().casefold()

    @staticmethod
    def _email_address(value: str) -> str:
        match = re.search(r"<([^<>]+@[^<>]+)>", value or "")
        return (match.group(1) if match else value).strip()

    @staticmethod
    def _strip_accents(value: str) -> str:
        return "".join(
            char for char in unicodedata.normalize("NFD", value) if unicodedata.category(char) != "Mn"
        )

    @staticmethod
    def _bounded(value: str) -> str:
        return (value or "")[:MAX_TEXT_SIZE]

    @staticmethod
    def _search(pattern: str, value: str) -> re.Match[str] | None:
        if not pattern or len(pattern) > MAX_PATTERN_SIZE:
            return None
        try:
            return re.search(pattern, value)
        except re.error as err:
            raise ParserError(f"Invalid parser regular expression: {err}") from err
