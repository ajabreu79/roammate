"""University reference data for the registration dropdown (FR-AUTH-013).

A static list keeps registration working without a third-party API. Replace
`UNIVERSITIES` with a database table or an external feed if the list needs to
grow past what is reasonable to ship in source.
"""

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

_NAMES = {name for name, _ in UNIVERSITIES}


def countries():
    return sorted({country for _, country in UNIVERSITIES})


def search(query=None, country=None, limit=50):
    """Names matching a substring query, optionally narrowed to one country."""
    needle = (query or "").strip().lower()
    wanted = (country or "").strip()

    results = []
    for name, uni_country in UNIVERSITIES:
        if wanted and uni_country != wanted:
            continue
        if needle and needle not in name.lower():
            continue
        results.append({"name": name, "country": uni_country})

    results.sort(key=lambda row: row["name"])
    return results[:limit]


def is_known(name):
    """Guard against a hand-crafted POST naming a university we do not list."""
    return name in _NAMES
