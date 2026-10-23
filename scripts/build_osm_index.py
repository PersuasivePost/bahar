"""Build the local OSM index (data/osm_index.sqlite) from Geofabrik *.osm.pbf extracts.

    OSM_DIR=~/osm uv run python scripts/build_osm_index.py

For every extract: `osmium tags-filter` for exactly the tags the Overpass venue and context
queries use (selectors from config/activities.py and config/osmtags.py), `osmium export` to
GeoJSON sequence, then one SQLite file with an R-tree on bounding boxes and WKB geometry.
Only stdlib sqlite3 + shapely. The index is written to a temp file and moved into place."""

import json
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import shapely
from shapely.geometry import shape

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # no installed package

from bahar.config import osmtags  # noqa: E402
from bahar.config.settings import get_settings  # noqa: E402
from bahar.data import localosm  # noqa: E402

KEEP_CONTEXT_TAGS = ("name", "ref", "highway", "landuse", "leisure", "natural", "waterway", "power",
                     "plant:source")  # fmt: skip
TYPES = {"n": "node", "w": "way", "r": "relation"}
SCHEMA = """
create table extracts (name text, south real, west real, north real, east real,
                       extract_date text, built_at text);
create table features (id integer primary key, osm text unique, venue integer not null,
                       ctx text, tags text not null, geom blob not null);
create table coverage (cx integer, cy integer, primary key (cx, cy)) without rowid;
create virtual table rtree using rtree(id, minlon, maxlon, minlat, maxlat);
"""


def osmium(*args: str) -> str:
    return subprocess.run(["osmium", *args], check=True, capture_output=True, text=True).stdout.strip()


def extract_date(pbf: Path) -> str:
    """Replication timestamp of the extract, else the file's date."""
    stamp = osmium("fileinfo", "-g", "header.option.osmosis_replication_timestamp", str(pbf))
    return stamp[:10] if stamp else datetime.fromtimestamp(pbf.stat().st_mtime, UTC).strftime("%Y-%m-%d")


def rows(seq: Path, outlines_of: set[str] | None):
    """(osm id, venue flag, context scope, stored tags, geometry) for every feature of an export
    that the index keeps."""
    with seq.open(encoding="utf-8") as f:
        for line in f:
            line = line.lstrip("\x1e").strip()
            if not line:
                continue
            feat = json.loads(line)
            tags: dict[str, str] = feat.get("properties") or {}
            fid = feat.get("id") or ""
            if not tags or fid[:1] not in ("n", "w", "a"):
                continue
            osm, is_way = osm_id(fid)
            if outlines_of is not None and osm in outlines_of and not linear(tags):
                continue  # the outline of a polygon already stored from the areas export
            venue = localosm.is_venue(tags)
            ctx = osmtags.context_scope(tags, is_way)
            if not venue and ctx is None:
                continue
            geom = shape(feat["geometry"])
            if geom.is_empty:
                continue
            if not venue:  # context-only rows keep just the tags the context parser reads
                tags = {k: v for k, v in tags.items() if k in KEEP_CONTEXT_TAGS}
            yield osm, venue, ctx, tags, geom


def load_extract(db: sqlite3.Connection, pbf: Path, tmp: Path) -> tuple[int, list[float]]:
    """Returns (new features, [south, west, north, east] of what was stored)."""
    filtered = tmp / f"{pbf.stem}.filtered.osm.pbf"
    exprs = [*localosm.osmium_venue_filters(), *osmtags.osmium_context_filters()]
    t0 = time.time()
    osmium("tags-filter", str(pbf), *exprs, "-o", str(filtered), "--overwrite")
    # Two exports: points + areas (closed ways, multipolygon relations), and ways as lines. A
    # way exported as an area also appears as its outline; that duplicate is skipped.
    exports = {}
    for kind, types in (("areas", "point,polygon"), ("lines", "linestring")):
        exports[kind] = tmp / f"{pbf.stem}.{kind}.geojsonseq"
        osmium("export", str(filtered), "-f", "geojsonseq", "--add-unique-id=type_id",
               f"--geometry-types={types}", "-o", str(exports[kind]), "--overwrite")
    print(f"  filtered + exported in {time.time() - t0:.0f} s", flush=True)

    inserted, batch = 0, []
    bbox = [90.0, 180.0, -90.0, -180.0]
    areas: set[str] = set()  # ways exported as polygons: their line export is a duplicate outline
    for kind, seq in exports.items():
        for osm, venue, ctx, tags, geom in rows(seq, areas if kind == "lines" else None):
            if kind == "areas":
                areas.add(osm)
            minlon, minlat, maxlon, maxlat = geom.bounds
            bbox = [min(bbox[0], minlat), min(bbox[1], minlon), max(bbox[2], maxlat), max(bbox[3], maxlon)]
            if maxlon - minlon < localosm.CELL_DEG and maxlat - minlat < localosm.CELL_DEG:
                cx, cy = localosm.cell((minlat + maxlat) / 2, (minlon + maxlon) / 2)
                db.execute("insert or ignore into coverage values (?, ?)", (cx, cy))
            batch.append((osm, int(venue), ctx, json.dumps(tags, ensure_ascii=False),
                          shapely.to_wkb(geom), minlon, maxlon, minlat, maxlat))  # fmt: skip
            if len(batch) >= 20_000:
                inserted += flush(db, batch)
                print(f"  {inserted:,} features", flush=True)
    inserted += flush(db, batch)
    return inserted, bbox


def osm_id(fid: str) -> tuple[str, bool]:
    """("way/123", True) from osmium's unique id: n<id>, w<id>, or a<area id> where an area id
    is 2 x way id (closed way) or 2 x relation id + 1 (multipolygon)."""
    if fid[0] == "a":
        n = int(fid[1:])
        return (f"way/{n // 2}", True) if n % 2 == 0 else (f"relation/{n // 2}", False)
    return f"{TYPES[fid[0]]}/{fid[1:]}", fid[0] == "w"


def linear(tags: dict[str, str]) -> bool:
    return "highway" in tags or "waterway" in tags or tags.get("natural") == "coastline"


def flush(db: sqlite3.Connection, batch: list[tuple]) -> int:
    n = 0
    for osm, venue, ctx, tags, wkb, *box in batch:
        cur = db.execute("insert or ignore into features (osm, venue, ctx, tags, geom) values (?,?,?,?,?)",
                         (osm, venue, ctx, tags, wkb))  # fmt: skip
        if cur.rowcount:  # an object in two overlapping extracts is stored once
            db.execute("insert into rtree values (?,?,?,?,?)", (cur.lastrowid, *box))
            n += 1
    db.commit()
    batch.clear()
    return n


def main() -> None:
    osm_dir = Path(get_settings().osm_dir).expanduser()
    pbfs = sorted(osm_dir.glob("*.osm.pbf"))
    if not pbfs:
        sys.exit(f"no *.osm.pbf in {osm_dir} (set OSM_DIR)")
    target = localosm.index_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_db = target.with_suffix(".building")
    tmp_db.unlink(missing_ok=True)
    db = sqlite3.connect(tmp_db)
    db.executescript(SCHEMA)
    with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
        for pbf in pbfs:
            date = extract_date(pbf)
            print(f"{pbf.name}: extract date {date}", flush=True)
            n, (south, west, north, east) = load_extract(db, pbf, Path(tmp))
            db.execute("insert into extracts values (?,?,?,?,?,?,?)",
                       (pbf.name, south, west, north, east, date, datetime.now(UTC).isoformat()))  # fmt: skip
            db.commit()
            print(f"  {n:,} new features; coverage bbox S{south:.2f} W{west:.2f} N{north:.2f} E{east:.2f}", flush=True)
    db.execute("create index features_venue on features(venue) where venue = 1")
    db.execute("create index features_ctx on features(ctx) where ctx is not null")
    db.commit()
    db.close()
    tmp_db.replace(target)
    print(f"index written: {target} ({target.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
