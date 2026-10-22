"""University reference data for the registration dropdown (FR-AUTH-013).

The design doc gives University its own table, keyed by (name, country), so
this list is seed data rather than the source of truth -- queries go to the
table. Replace the seed with an external feed if the list outgrows source.
"""

from . import db
from . import model

UNIVERSITIES = [
    ("Case Western Reserve University", "United States"),
    ("Ohio State University", "United States"),
    ("University of Michigan", "United States"),
    ("Massachusetts Institute of Technology", "United States"),
    ("Stanford University", "United States"),
    ("Harvard University", "United States"),
    ("University of California, Berkeley", "United States"),
    ("Georgia Institute of Technology", "United States"),
    ("University of Texas at Austin", "United States"),
    ("New York University", "United States"),
    ("University of Toronto", "Canada"),
    ("McGill University", "Canada"),
    ("University of British Columbia", "Canada"),
    ("University of Waterloo", "Canada"),
    ("University of Oxford", "United Kingdom"),
    ("University of Cambridge", "United Kingdom"),
    ("Imperial College London", "United Kingdom"),
    ("University of Edinburgh", "United Kingdom"),
    ("University College London", "United Kingdom"),
    ("Trinity College Dublin", "Ireland"),
    ("Technical University of Munich", "Germany"),
    ("Ludwig Maximilian University of Munich", "Germany"),
    ("Heidelberg University", "Germany"),
    ("Delft University of Technology", "Netherlands"),
    ("University of Amsterdam", "Netherlands"),
    ("ETH Zurich", "Switzerland"),
    ("EPFL", "Switzerland"),
    ("Sorbonne University", "France"),
    ("Sciences Po", "France"),
    ("Polytechnic University of Milan", "Italy"),
    ("University of Bologna", "Italy"),
    ("Autonomous University of Madrid", "Spain"),
    ("University of Barcelona", "Spain"),
    ("Lund University", "Sweden"),
    ("KTH Royal Institute of Technology", "Sweden"),
    ("University of Copenhagen", "Denmark"),
    ("University of Melbourne", "Australia"),
    ("University of Sydney", "Australia"),
    ("Australian National University", "Australia"),
    ("University of Auckland", "New Zealand"),
    ("University of Tokyo", "Japan"),
    ("Kyoto University", "Japan"),
    ("Seoul National University", "South Korea"),
    ("National University of Singapore", "Singapore"),
    ("Tsinghua University", "China"),
    ("Peking University", "China"),
    ("University of Hong Kong", "Hong Kong"),
    ("Indian Institute of Technology Bombay", "India"),
    ("University of Cape Town", "South Africa"),
    ("University of São Paulo", "Brazil"),
    ("National Autonomous University of Mexico", "Mexico"),
]



def seed(session=None):
    """Insert any seed universities the table is missing. Idempotent."""
    session = session or db.session
    existing = {
        (row.name, row.country)
        for row in session.execute(db.select(model.University)).scalars()
    }
    added = 0
    for name, country in UNIVERSITIES:
        if (name, country) not in existing:
            session.add(model.University(name=name, country=country))
            added += 1
    if added:
        session.commit()
    return added


def countries():
    query = db.select(model.University.country).distinct().order_by(model.University.country)
    return list(db.session.execute(query).scalars())


def search(query=None, country=None, limit=50):
    """Universities matching a substring, optionally narrowed to one country.

    FR-AUTH-013. On PostgreSQL the trigram index on University.name serves the
    ILIKE; on SQLite this is a scan over a short table.
    """
    statement = db.select(model.University)

    needle = (query or "").strip()
    if needle:
        statement = statement.where(model.University.name.ilike(f"%{needle}%"))

    wanted = (country or "").strip()
    if wanted:
        statement = statement.where(model.University.country == wanted)

    rows = db.session.execute(
        statement.order_by(model.University.name).limit(limit)
    ).scalars()
    return [{"id": row.id, "name": row.name, "country": row.country} for row in rows]


def get_by_name(name):
    """The University row with this exact name, or None."""
    if not (name or "").strip():
        return None
    query = db.select(model.University).where(model.University.name == name.strip())
    return db.session.execute(query).scalars().first()


def is_known(name):
    """Guard against a hand-crafted POST naming a university we do not list."""
    return get_by_name(name) is not None
