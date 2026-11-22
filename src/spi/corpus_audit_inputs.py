"""Read and fingerprint the scientific inputs without changing them."""

from contextlib import ExitStack, closing, contextmanager, nullcontext
from dataclasses import dataclass
from hashlib import file_digest, sha256
from pathlib import Path
import re
import shutil
import sqlite3

import pymupdf

from .build_scope_sheet import FIELDS, QUEUE_FIELDS, read_scope_rows
from .review_scope import PUBLICATIONS, TRACKS, check_scope_conflicts, validate_review
from .storage import Store


DEFAULTS = {
    "state": "outputs/p0-21-test-local/state",
    "queue": "outputs/p0-21-test-local/scope-human-review.csv",
    "approval": "outputs/p0-22-release/scope-approval.csv",
    "baseline": "outputs/test-corpus-intake/previously-reviewed-scopes.json",
}
RELEASE = Path("outputs/p0-22-release")
LOCKS = ("batch-lock.sqlite3", "write-lock.sqlite3")
IDENTITY_HASH = re.compile(r"[a-f0-9]{64}")


class AuditBlocked(RuntimeError):
    """A necessary source is unavailable or changed during the audit."""


@dataclass(frozen=True)
class InputFile:
    path: Path
    sha256: str
    size: int


def fingerprint(path):
    with path.open("rb") as file:
        digest = file_digest(file, "sha256")
        return InputFile(path, digest.hexdigest(), file.tell())


def is_coordination_file(name):
    return any(name == lock or name.startswith(lock + "-") for lock in LOCKS)


def tree(folder, *, include_coordination=True):
    inventory = {}
    for path in sorted(folder.rglob("*")):
        name = path.relative_to(folder).as_posix()
        if (include_coordination or not is_coordination_file(name)) and path.is_file():
            inventory[name] = fingerprint(path)
    return inventory


def review_inputs(args):
    sources = {name: fingerprint(getattr(args, name)) for name in ("queue", "approval", "baseline")}
    if args.compare_readiness not in (None, True):
        sources["comparison"] = fingerprint(args.compare_readiness)
    return sources


def protection_inventory(root):
    """Observe local release material by role without relying on Git tracking."""
    release = root / RELEASE
    paths = {
        "release_config": [
            *(release / name for name in ("export-final.ps1", "review_scope.py", "review-metadata.json")),
            *(root / name for name in ("passport.md", "src/spi/csv_schema.py", "src/spi/store_export.py",
                                      "src/spi/release_readiness.py", "src/spi/review_scope.py")),
            *(root / f"outputs/contracts/{track}/tools/check_submission.py" for track in TRACKS),
            root / "outputs/contracts/rag/submission_template/schema.json",
        ],
        "release_candidate_csv": sorted(release.glob("candidate-*/*/*.csv"))
        + sorted(release.glob("candidate/*/*.csv")),
        "pilot_official_format_csv": sorted(root.glob("outputs/p0-21-test-local/d001-official/*/*.csv")),
    }
    return {f"{role}/{path.relative_to(root).as_posix()}": fingerprint(path)
            for role, files in paths.items() for path in files if path.is_file()}


@contextmanager
def quiescent_store(state, existing):
    # Reserve the existing SQLite coordination locks. No SQL writes or lock-file
    # creation. BEGIN IMMEDIATE conflicts with both kinds of Store writers.
    for name in LOCKS:
        if any(path.startswith(name + "-") for path in existing):
            raise AuditBlocked(f"Store coordination sidecar exists for {name}; retry after Store writes finish")
    with ExitStack() as stack:
        for name in LOCKS:
            if name not in existing:
                continue
            uri = (state / name).resolve().as_uri() + "?mode=rw"
            try:
                lock = stack.enter_context(closing(sqlite3.connect(uri, uri=True, timeout=0)))
                lock.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise AuditBlocked(f"Cannot reserve the existing {name}; retry after Store writes finish") from exc
        yield


def capture(args, snapshot):
    if not (args.state / "documents.json").is_file():
        raise AuditBlocked("Store registry documents.json is unavailable (--state)")
    for name in ("queue", "approval", "baseline"):
        if not getattr(args, name).is_file():
            raise AuditBlocked(f"Scope input --{name} is unavailable")
    if isinstance(args.compare_readiness, Path) and not args.compare_readiness.is_file():
        raise AuditBlocked("Comparison report supplied to --compare-readiness is unavailable")
    previous = args.root / RELEASE / "release-state"
    store_sources = tree(args.state)
    previous_sources = tree(previous)
    if previous.exists() and "documents.json" not in previous_sources:
        raise ValueError("Existing release-state has no document registry")
    review_sources = review_inputs(args)
    groups = {"store": store_sources, "previous_release_state": previous_sources, "review": review_sources}

    def same_scientific_files():
        # Opening and closing the reserved SQLite files independently can release
        # SQLite's POSIX advisory locks. Only hash them outside the reservation.
        return all(
            tree(folder, include_coordination=False)
            == {name: item for name, item in sources.items() if not is_coordination_file(name)}
            for folder, sources in ((args.state, store_sources), (previous, previous_sources))
        )

    previous_lock = (quiescent_store(previous, previous_sources)
                     if previous.resolve() != args.state else nullcontext())
    with quiescent_store(args.state, store_sources), previous_lock:
        if not same_scientific_files():
            raise AuditBlocked("Store changed before snapshot; retry after writes finish")
        for group, sources in groups.items():
            (snapshot / group).mkdir()
            for name, source in sources.items():
                if group != "review" and (name.endswith("_pages.json") or not name.endswith(".json")):
                    continue
                copy = snapshot / group / name
                copy.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source.path, copy)
                if fingerprint(copy).sha256 != source.sha256:
                    raise AuditBlocked(f"{group} changed while being copied; retry after writes finish")
        if not same_scientific_files() or review_inputs(args) != review_sources:
            raise AuditBlocked("Store or review inputs changed during snapshot; retry after writes finish")
    if tree(args.state) != store_sources or tree(previous) != previous_sources:
        raise AuditBlocked("Store changed at snapshot boundary; retry after writes finish")
    return groups


def validated_registry(store):
    rows = store.documents()
    docs = {}
    for doc in rows:
        key, domain, pid = (doc[k] for k in ("document_id", "domain", "publication_id"))
        if (key in docs or domain not in TRACKS or key != f"{domain}:{pid}"
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", pid)
                or not IDENTITY_HASH.fullmatch(doc["sha256"])
                or type(doc["page_count"]) is not int or doc["page_count"] < 1):
            raise ValueError("Duplicate or invalid document identity in Store registry")
        docs[key] = doc
    return docs


def read_decisions(args, snapshot, docs, store):
    review = snapshot / "review"
    queue = read_scope_rows((review / "queue").read_bytes(), "review queue", QUEUE_FIELDS)
    sheet = read_scope_rows((review / "approval").read_bytes(), "approval worksheet", FIELDS, exact=True)
    baseline = review / "baseline"
    scopes = store.publication_scopes()
    decisions = validate_review(docs, scopes, queue, sheet, baseline, args.root)
    previous = Store(snapshot / "previous_release_state")
    if (previous.root / "documents.json").is_file():
        if validated_registry(previous) != docs:
            raise ValueError("Existing release-state has a different document registry")
        check_scope_conflicts(previous.publication_scopes(), docs, decisions)
    return decisions, scopes, queue, sheet


def select_pdfs(args, docs):
    sources = {}
    tracks = TRACKS if args.domain == "all" else (args.domain,)
    for track in tracks:
        folder = args.root / "data" / track / "test"
        if not folder.is_dir():
            raise AuditBlocked(f"{track}: data/{track}/test is unavailable")
        pdfs = {}
        for path in sorted(folder.iterdir()):
            if not path.is_file() or path.suffix.lower() != ".pdf":
                continue
            if args.publication_id and path.stem != args.publication_id:
                continue
            if path.stem in pdfs:
                raise ValueError(f"{track}:{path.stem}: duplicate original PDF")
            pdfs[path.stem] = path
        registered = {d["publication_id"] for d in docs.values() if d["domain"] == track}
        expected = set(PUBLICATIONS) if args.publication_id is None else {args.publication_id}
        if args.publication_id and args.domain == "all" and not pdfs and not registered & expected:
            continue
        if expected - pdfs.keys() or expected - registered:
            raise AuditBlocked(f"{track}: missing test PDFs or registrations ({len(expected - pdfs.keys())} PDFs, "
                               f"{len(expected - registered)} registrations)")
        if not args.publication_id and (pdfs.keys() != expected or registered != expected):
            raise ValueError(f"{track}: full test track must contain exactly P001–P090")
        sources.update({f"{track}:{pid}": path for pid, path in pdfs.items()})
    if not sources:
        raise AuditBlocked("No original PDF and registration match the requested publication")
    return sources


def verify_pdfs(args, docs, sources):
    pdf_inventory, metadata = {}, {}
    for key, path in sorted(sources.items()):
        doc = docs[key]
        if Path(doc["file_path"]).resolve() != path.resolve():
            raise ValueError(f"{key}: registry path differs from the original PDF")
        original = fingerprint(path)
        if original.sha256 != doc["sha256"]:
            raise ValueError(f"{key}: original PDF SHA-256 differs from the registry")
        pdf_inventory[key] = original
        if args.verify_pdfs:
            content = path.read_bytes()
            if sha256(content).hexdigest() != original.sha256:
                raise AuditBlocked(f"{key}: original PDF changed during inspection")
            try:
                with pymupdf.open(stream=content, filetype="pdf") as pdf:
                    if pdf.needs_pass or pdf.page_count != doc["page_count"]:
                        raise ValueError(f"{key}: original PDF is encrypted or its page count differs from the registry")
                    metadata[key] = pdf.metadata or {}
            except (RuntimeError, pymupdf.FileDataError) as error:
                raise ValueError(f"{key}: original PDF could not be opened ({type(error).__name__})") from error
    return pdf_inventory, metadata


def check_unchanged(args, groups, originals, protection):
    final = {
        "store": tree(args.state),
        "previous_release_state": tree(args.root / RELEASE / "release-state"),
        "review": review_inputs(args),
    }
    if final != groups:
        raise AuditBlocked("Store or review inputs changed during audit; retry after writes finish")
    if any(source != fingerprint(source.path) for source in originals.values()):
        raise AuditBlocked("Original PDFs changed during audit; retry with stable originals")
    if protection != protection_inventory(args.root):
        raise AuditBlocked("Release files changed during audit; retry after writes finish")
