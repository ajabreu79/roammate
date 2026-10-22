"""FR-ACE-001 reference data: the activity types offered on the creation form.

NFR-SOFT-009: a new type is a row, not a schema change, so this list can grow
without a migration. Seeded by `flask seed-activity-types`, the same way
universities are.
"""

from . import db
from . import model

ACTIVITY_TYPES = (
    "Concert",
    "Dining",
    "Game night",
    "Hiking",
    "Movie",
    "Museum",
    "Party",
    "Road trip",
    "Shopping",
    "Sports game",
    "Study group",
    "Volunteering",
    "Other",
)


def seed(session=None):
    """Insert any seed activity types the table is missing. Idempotent."""
    session = session or db.session
    existing = set(session.execute(db.select(model.ActivityType.name)).scalars())
    added = 0
    for name in ACTIVITY_TYPES:
        if name not in existing:
            session.add(model.ActivityType(name=name))
            added += 1
    if added:
        session.commit()
    return added


def all_types():
    """Every activity type, alphabetical, for the form and the browse filter."""
    return db.session.execute(
        db.select(model.ActivityType).order_by(model.ActivityType.name)
    ).scalars().all()
