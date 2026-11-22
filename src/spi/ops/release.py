"""Bind Trivy JSON, CycloneDX and release test results to the OCI image manifests."""

import argparse
import hashlib
import json
import os
import re
import tarfile
import tomllib
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlsplit

IMAGES = ("runtime", "proxy")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
REVISION = re.compile(r"[0-9a-f]{40}")
CVE = re.compile(r"CVE-[0-9]{4}-[0-9]{4,}")
SEVERITIES = {"CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"}
RELEASE_TESTS = {"build", "runtime_controls", "trivy", "deployment_integration", "regression"}
EXCEPTION_KEYS = {"cve", "package", "image", "version", "target_type", "justification",
                  "owner", "assessed_on", "review_by", "advisory", "invalidated_if", "scope"}


def read_json(path):
    def reject_duplicates(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError(f"Duplicate field {key}")
            result[key] = value
        return result

    with path.open(encoding="utf-8") as stream:
        return json.load(stream, object_pairs_hook=reject_duplicates)


def sha256_file(path):
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def checked_time(value):
    timestamp = datetime.fromisoformat(value)
    if timestamp.utcoffset() is None:
        raise ValueError("A timezone is required for evidence")
    return timestamp.astimezone(UTC)


def https_reference(value):
    url = urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ValueError("An HTTPS advisory or primary evidence URL is required")
    return value


def oci_subject(path, revision=None):
    with tarfile.open(path) as archive:
        def read(name):
            return archive.extractfile(name).read()

        def blob(descriptor):
            digest = descriptor["digest"]
            if not DIGEST.fullmatch(digest):
                raise ValueError("Invalid OCI descriptor")
            raw = read("blobs/sha256/" + digest[7:])
            if len(raw) != descriptor["size"] or "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError("OCI blob does not match its descriptor")
            return json.loads(raw)

        root, = json.loads(read("index.json"))["manifests"]
        manifest = blob(root)
        if "manifests" in manifest:
            subjects = [item for item in manifest["manifests"]
                        if item.get("platform") == {"architecture": "amd64", "os": "linux"}]
            subject, = subjects
            manifest = blob(subject)
        else:
            subject = root
        config = blob(manifest["config"])
        if (config["os"], config["architecture"]) != ("linux", "amd64"):
            raise ValueError("Unsupported OCI platform")
        if revision and config.get("config", {}).get("Labels", {}).get("org.opencontainers.image.revision") != revision:
            raise ValueError("OCI revision label does not match checkout")
    return {"oci_digest": subject["digest"], "archive_index_digest": root["digest"],
            "config_digest": manifest["config"]["digest"], "diff_ids": config["rootfs"]["diff_ids"]}


def exception_map(manifest, today):
    if set(manifest) != {"schema_version", "exceptions"} or manifest["schema_version"] != 1:
        raise ValueError("Unsupported OCI exception schema")
    entries = {}
    for row in manifest["exceptions"]:
        if (set(row) != EXCEPTION_KEYS or row["image"] not in IMAGES or not CVE.fullmatch(row["cve"])
                or row["scope"] not in ("candidate", "public")):
            raise ValueError("Invalid OCI exception")
        for key in ("package", "version", "target_type", "justification", "owner", "invalidated_if"):
            if not isinstance(row[key], str) or not row[key].strip() or "*" in row[key]:
                raise ValueError(f"Invalid exception {key}")
        https_reference(row["advisory"])
        if not date.fromisoformat(row["assessed_on"]) <= today < date.fromisoformat(row["review_by"]):
            raise ValueError(f"Inactive exception {row['cve']}")
        key = (row["image"], row["cve"], row["package"], row["version"], row["target_type"])
        if key in entries:
            raise ValueError(f"Duplicate OCI exception {key}")
        entries[key] = row
    return entries


def scan_result(image, report, subject, exceptions, *, public=False):
    if (report["SchemaVersion"] != 2 or report["ArtifactType"] != "container_image"
            or report["Metadata"]["ImageID"] != subject["config_digest"]
            or report["Metadata"]["DiffIDs"] != subject["diff_ids"]):
        raise ValueError(f"{image} scan does not describe the expected OCI subject")
    checked_time(report["CreatedAt"])
    targets = report["Results"]
    if not {"os-pkgs", "lang-pkgs"} <= {target["Class"] for target in targets}:
        raise ValueError(f"{image} scan lacks OS or language inventory")
    totals = Counter({name: 0 for name in SEVERITIES})
    blockers, high = [], []
    for target in targets:
        for row in target.get("Vulnerabilities", []):
            cve, package, version = (row[key] for key in ("VulnerabilityID", "PkgName", "InstalledVersion"))
            severity = row["Severity"]
            if severity not in SEVERITIES:
                raise ValueError("Unknown Trivy severity")
            totals[severity] += 1
            if severity == "CRITICAL":
                blockers.append(f"{image} {cve} {package}: critical")
            if severity == "HIGH":
                key = (image, cve, package, version, target["Type"])
                exception = exceptions.get(key)
                fix = row.get("FixedVersion")
                if fix or not exception or (public and exception["scope"] != "public"):
                    blockers.append(f"{image} {cve} {package} {version}: "
                                    + ("fix available" if fix else "no applicable approval"))
                high.append({"cve": cve, "package": package, "installed_version": version,
                             "target_type": target["Type"], "fixed_version": fix,
                             "approval": exception["scope"] if exception and not fix else None})
    secrets = sum(len(target.get("Secrets", [])) for target in targets)
    if secrets:
        blockers.append(f"{image}: {secrets} secret finding(s)")
    return {"scan_at": report["CreatedAt"], "report_id": report["ReportID"], "summary": dict(totals),
            "secret_count": secrets, "high": high, "blockers": blockers}


def verify_sbom(path, subject):
    report = read_json(path)
    component = report["metadata"]["component"]
    props = component["properties"]
    ids = [p["value"] for p in props if p["name"] == "aquasecurity:trivy:ImageID"]
    layers = [p["value"] for p in props if p["name"] == "aquasecurity:trivy:DiffID"]
    if (report["bomFormat"] != "CycloneDX" or component["type"] != "container" or not report["components"]
            or ids != [subject["config_digest"]] or Counter(layers) != Counter(subject["diff_ids"])):
        raise ValueError("SBOM does not describe the expected OCI subject")
    return {"id": report["serialNumber"], "sha256": sha256_file(path)}


def evaluate_release(directory, revision, repository, workflow, checks, specification, exception_file, *, now=None):
    if not REVISION.fullmatch(revision):
        raise ValueError("Expected full Git SHA")
    now = now or datetime.now(UTC)
    exceptions = exception_map(read_json(exception_file), now.date())
    images = {}
    for image in IMAGES:
        archive = directory / f"{image}.tar"
        subject = oci_subject(archive, revision)
        scan = directory / f"{image}.json"
        result = scan_result(image, read_json(scan), subject, exceptions)
        images[image] = {**subject, "archive_sha256": sha256_file(archive), "scan": {
            **result, "sha256": sha256_file(scan)},
            "sbom": verify_sbom(directory / f"{image}.cdx.json", subject)}
    tests = {name: {"result": status, "workflow_run": workflow,
                    "image_digests": {image: entry["oci_digest"] for image, entry in images.items()}}
             for name, status in checks.items()}
    ready = set(tests) == RELEASE_TESTS and all(item["result"] == "success" for item in tests.values())
    ready &= not any(entry["scan"]["blockers"] for entry in images.values())
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    bases = {name: re.findall(r"(?:FROM |ARG PYTHON_IMAGE=)([^\s]+@sha256:[0-9a-f]{64})",
                             Path(inputs[0]).read_text(encoding="utf-8"))
             for name, inputs in specification["build_inputs"].items()}
    toolchain = directory / "toolchain.txt"
    return {**specification, "project_version": project["version"], "revision": revision,
            "repository": repository, "workflow_run": workflow, "generated_at": now.isoformat(),
            "base_images": bases, "toolchain": toolchain.read_text(encoding="utf-8"),
            "images": images, "tests": tests, "exceptions_sha256": sha256_file(exception_file),
            "release_readiness": "READY" if ready else "BLOCKED", "public_launch": "NOT READY"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--repository", default=os.getenv("GITHUB_REPOSITORY", "local"))
    parser.add_argument("--workflow", default=os.getenv("GITHUB_RUN_ID", "local"))
    parser.add_argument("--check", action="append", default=[], metavar="NAME=RESULT")
    args = parser.parse_args()
    report = evaluate_release(
        args.directory, args.revision, args.repository, args.workflow,
        dict(item.split("=", 1) for item in args.check), read_json(Path("deploy/release-manifest.json")),
        Path("deploy/security/oci-exceptions.json"),
    )
    (args.directory / "release-manifest.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for image, result in report["images"].items():
        print(image, result["oci_digest"], result["scan"]["summary"])
        for blocker in result["scan"]["blockers"]:
            print(blocker)
    print(f"RELEASE HARDENING: {report['release_readiness']}")
    return int(report["release_readiness"] != "READY")


if __name__ == "__main__":
    raise SystemExit(main())
