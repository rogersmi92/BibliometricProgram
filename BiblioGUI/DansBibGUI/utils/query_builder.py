from __future__ import annotations

from dataclasses import dataclass, field


FIELD_TARGETS = ("All Fields", "Topic", "Title", "Abstract", "Title OR Abstract")

FIELD_PREFIXES = {
    "All Fields": "ALL",
    "Topic": "TS",
    "Title": "TI",
    "Abstract": "AB",
}

BLOCK_LABELS = {
    "geography": "Geography / population terms",
    "topic": "Main topic / disease terms",
    "intervention": "Intervention / method terms",
    "outcomes": "Outcomes / access terms",
    "exclusion": "Exclusion terms",
}

PRESETS = {
    "Blank custom query": {
        "geography": "",
        "topic": "",
        "intervention": "",
        "outcomes": "",
        "exclusion": "",
        "start_year": "",
        "end_year": "",
    },
    "Rural Texas cancer care": {
        "geography": "west texas, texas, rural, southwest united states, rural populations, north america",
        "topic": "cancer treatment, cancer, cancer diagnosis, cancer screening, neoplasms, oncology",
        "intervention": "",
        "outcomes": "delivery of health care, mortality, survival, outcomes, epidemiology, healthcare quality, healthcare access, survivorship, access to care, health services accessibility",
        "exclusion": "animal",
        "start_year": "",
        "end_year": "",
    },
    "Telehealth cancer treatment": {
        "geography": "west texas, texas, rural, southwest united states, rural populations, north america",
        "topic": "cancer treatment, cancer, cancer diagnosis, cancer screening, neoplasms, oncology",
        "intervention": "telemedicine, telehealth, telecommunication, e-health, ehealth, virtual health, virtual consultation, virtual medicine, mobile health, remote consultation",
        "outcomes": "delivery of health care, mortality, survival, outcomes, epidemiology, healthcare quality, healthcare access, survivorship, access to care, health services accessibility",
        "exclusion": "animal",
        "start_year": "",
        "end_year": "",
    },
    "Rural health access/outcomes": {
        "geography": "rural, rural populations, west texas, texas, southwest united states",
        "topic": "health services, healthcare access, access to care, health services accessibility",
        "intervention": "delivery of health care, telehealth, mobile health, remote consultation",
        "outcomes": "mortality, survival, outcomes, healthcare quality, survivorship, epidemiology",
        "exclusion": "animal",
        "start_year": "",
        "end_year": "",
    },
}


@dataclass(frozen=True)
class ConceptBlock:
    key: str
    terms: list[str] = field(default_factory=list)
    field_target: str = "Topic"


@dataclass(frozen=True)
class QueryBuildResult:
    preview: str
    machine_query: str
    has_query: bool
    errors: list[str] = field(default_factory=list)


def parse_terms(raw_terms: str) -> list[str]:
    seen: set[str] = set()
    parsed: list[str] = []
    for raw in raw_terms.replace("\n", ",").split(","):
        term = " ".join(raw.strip().split())
        if not term:
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        parsed.append(term)
    return parsed


def quote_term(term: str) -> str:
    escaped = term.replace('"', '\\"')
    return f'"{escaped}"' if " " in escaped or "-" in escaped else escaped


def build_or_block(terms: list[str]) -> str:
    return " OR ".join(quote_term(term) for term in terms)


def target_expression(terms: list[str], field_target: str) -> str:
    block = build_or_block(terms)
    if not block:
        return ""
    if field_target == "Title OR Abstract":
        return f"(TI=({block}) OR AB=({block}))"
    prefix = FIELD_PREFIXES.get(field_target, "TS")
    return f"{prefix}=({block})"


def build_and_combination(lines: list[str]) -> str:
    return " AND ".join(lines)


def add_year_filter(expression: str, start_year: int | None, end_year: int | None) -> str:
    year_expression = year_filter_expression(start_year, end_year)
    if not year_expression:
        return expression
    return f"{expression} AND {year_expression}" if expression else year_expression


def add_not_exclusions(expression: str, exclusion_expression: str) -> str:
    if not exclusion_expression:
        return expression
    return f"{expression} AND NOT {exclusion_expression}" if expression else f"NOT {exclusion_expression}"


def year_filter_expression(start_year: int | None, end_year: int | None) -> str:
    if start_year is None and end_year is None:
        return ""
    if start_year is not None and end_year is not None:
        return f"PY=({start_year}-{end_year})"
    if start_year is not None:
        return f"PY=({start_year}-*)"
    return f"PY=(*-{end_year})"


def build_wos_numbered_query(
    blocks: list[ConceptBlock],
    start_year: int | None = None,
    end_year: int | None = None,
) -> QueryBuildResult:
    errors = validate_years(start_year, end_year)
    include_blocks = [block for block in blocks if block.key != "exclusion" and block.terms]
    exclusion_blocks = [block for block in blocks if block.key == "exclusion" and block.terms]

    numbered_lines: list[tuple[str, str, str]] = []
    for block in include_blocks:
        numbered_lines.append((block.key, BLOCK_LABELS.get(block.key, block.key.title()), target_expression(block.terms, block.field_target)))

    preview_lines: list[str] = []
    machine_parts: list[str] = []
    for index, (_, label, expression) in enumerate(numbered_lines, start=1):
        preview_lines.append(f"#{index} {label}")
        preview_lines.append(expression)
        machine_parts.append(expression)

    combined_expression = build_and_combination(machine_parts)
    if machine_parts:
        preview_lines.append(f"#{len(preview_lines) // 2 + 1} Combine concept blocks")
        preview_lines.append(" AND ".join(f"#{idx}" for idx in range(1, len(machine_parts) + 1)))

    year_expression = year_filter_expression(start_year, end_year)
    if year_expression:
        preview_lines.append(f"#{len(preview_lines) // 2 + 1} Apply year filter")
        preview_lines.append(year_expression)
        combined_expression = add_year_filter(combined_expression, start_year, end_year)

    exclusion_expression = ""
    if exclusion_blocks:
        exclusion_terms = []
        for block in exclusion_blocks:
            exclusion_terms.extend(block.terms)
        exclusion_expression = target_expression(exclusion_terms, exclusion_blocks[0].field_target)
        preview_lines.append(f"#{len(preview_lines) // 2 + 1} Exclude terms")
        preview_lines.append(f"NOT {exclusion_expression}")
        combined_expression = add_not_exclusions(combined_expression, exclusion_expression)

    if not include_blocks and not errors:
        errors.append("Add at least one term to the guided query builder.")

    return QueryBuildResult(
        preview="\n".join(preview_lines),
        machine_query=combined_expression,
        has_query=bool(combined_expression and not errors),
        errors=errors,
    )


def validate_years(start_year: int | None, end_year: int | None) -> list[str]:
    errors: list[str] = []
    for label, year in (("Start year", start_year), ("End year", end_year)):
        if year is not None and (year < 1800 or year > 2100):
            errors.append(f"{label} must be between 1800 and 2100.")
    if start_year is not None and end_year is not None and start_year > end_year:
        errors.append("Start year must be earlier than or equal to end year.")
    return errors
