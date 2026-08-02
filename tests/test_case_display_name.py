"""Tests for Case.display_name (FERChronos UX refinement Step 2).

Covers the additive legal_first_name/legal_last_name/preferred_name
columns and the fallback/format logic in Case.display_name -- see the
Case model docstring for the exact rule.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import Case


def test_display_name_falls_back_to_label_when_name_fields_unset(sample_case: Case):
    """Every pre-Step-2 case row (and any student who never fills these
    fields in) must keep showing exactly what it always showed: `label`.
    """
    assert sample_case.legal_first_name is None
    assert sample_case.legal_last_name is None
    assert sample_case.preferred_name is None
    assert sample_case.display_name == sample_case.label


def test_display_name_uses_full_format_when_all_three_fields_set(db_session: Session):
    case = Case(
        label="Zeke Froiseth",
        legal_first_name="Isabella",
        legal_last_name="Froiseth",
        preferred_name="Izzy",
    )
    db_session.add(case)
    db_session.commit()

    assert case.display_name == "Isabella (Izzy) Froiseth"


def test_display_name_falls_back_when_only_some_fields_set(db_session: Session):
    """Partial data (e.g. only a legal first name entered so far) must not
    produce a malformed name -- it falls back to `label` until all three
    are present, same as the "none set" case.
    """
    case = Case(label="Partial Student", legal_first_name="OnlyFirst")
    db_session.add(case)
    db_session.commit()

    assert case.display_name == "Partial Student"


def test_display_name_never_shows_a_legal_prefix(db_session: Session):
    case = Case(
        label="Isabella Froiseth",
        legal_first_name="Isabella",
        legal_last_name="Froiseth",
        preferred_name="Izzy",
    )
    db_session.add(case)
    db_session.commit()

    assert "legal:" not in case.display_name.lower()
