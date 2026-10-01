"""Agent repos, the workspace that holds them, and the local package registry.

    workspace.yaml                    the workspace: which repos, where the registry and index live
    registry/<package>/<version>/     immutable published package versions (each one is an agent repo)
    repos/<project>/                  project agent repos

An agent repo:

    repo.yaml        manifest: name, version, kind (project|package), dependencies, exports
    knowledge.lock   pinned package versions + content hashes + type/skill versions (reproducible work)
    knowledge/       typed Markdown objects (any *.md in the repo is indexed; tests/ is skipped)
    types/           *.type.yaml
    methods/ skills/<name>/{SKILL.md,checks.yaml,tools.yaml,tests/} views/*.view.yaml
    queries/*.query.yaml  profiles/*.profile.yaml  migrations/*.migration.yaml  apps/<name>/app.yaml

Dependency resolution: a name that is another repo in the workspace resolves to it (live, no
version); otherwise it resolves through knowledge.lock (pinned) or, if unlocked, to the highest
registry version matching the constraint.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SKIP_DIRS = {"tests", "originals", ".git", ".knowledge", "__pycache__", "node_modules"}   # originals: ingested raw files
DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "knowledge"


class RepoError(Exception):
    pass


# ── semver ───────────────────────────────────────────────────────────────────

def vparse(v: str) -> tuple[int, int, int]:
    m = re.match(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(v).strip())
    if not m:
        raise RepoError(f"bad version '{v}'")
    return int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)


def satisfies(version: str, constraint: str | None) -> bool:
    c = (constraint or "*").strip()
    if c in ("*", "", "latest"):
        return True
    v = vparse(version)
    for part in [p.strip() for p in c.split(",") if p.strip()]:
        if part.startswith("^"):
            b = vparse(part[1:])
            upper = (b[0] + 1, 0, 0) if b[0] > 0 else (0, b[1] + 1, 0)
            if not (b <= v < upper):
                return False
        elif part.startswith("~"):
            b = vparse(part[1:])
            if not (b <= v < (b[0], b[1] + 1, 0)):
                return False
        elif part[:2] in (">=", "<=", "==") or part[:1] in (">", "<", "="):
            op = part[:2] if part[:2] in (">=", "<=", "==") else part[:1]
            b = vparse(part[len(op):])
            ok = {">=": v >= b, "<=": v <= b, ">": v > b, "<": v < b, "==": v == b, "=": v == b}[op]
            if not ok:
                return False
        elif v != vparse(part):
            return False
    return True


def tree_hash(path: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(path.rglob("*")):
        if p.is_file() and not any(part in (".knowledge", "__pycache__") for part in p.parts):
            h.update(str(p.relative_to(path)).encode())
            h.update(b"\0")
            h.update(p.read_bytes())
    return "sha256:" + h.hexdigest()


# ── manifests ────────────────────────────────────────────────────────────────

@dataclass
class Dependency:
    name: str
    version: str = "*"

    @classmethod
    def parse(cls, raw: Any) -> "Dependency":
        if isinstance(raw, str):
            name, _, ver = raw.partition("@")
            return cls(name.strip(), ver.strip() or "*")
        return cls(str(raw["name"]), str(raw.get("version", "*")))


@dataclass
class Repo:
    name: str
    path: Path
    version: str = "0.0.0"
    kind: str = "project"
    description: str = ""
    dependencies: list[Dependency] = field(default_factory=list)
    exports: dict[str, list[str]] = field(default_factory=dict)
    manifest: dict = field(default_factory=dict)
    source: str = "workspace"          # workspace | registry
    writable: bool = True

    @classmethod
    def load(cls, path: Path, source: str = "workspace") -> "Repo":
        mf = path / "repo.yaml"
        if not mf.exists():
            raise RepoError(f"{path}: no repo.yaml")
        raw = yaml.safe_load(mf.read_text()) or {}
        if "name" not in raw:
            raise RepoError(f"{mf}: manifest needs a name")
        return cls(name=str(raw["name"]), path=path, version=str(raw.get("version", "0.0.0")),
                   kind=str(raw.get("kind", "project")), description=str(raw.get("description", "")),
                   dependencies=[Dependency.parse(d) for d in raw.get("dependencies") or []],
                   exports={k: list(v or []) for k, v in (raw.get("exports") or {}).items()}, manifest=raw,
                   source=source, writable=source == "workspace")

    def markdown_files(self) -> list[Path]:
        out = []
        for root, dirs, files in os.walk(self.path):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
            out += [Path(root) / f for f in sorted(files) if f.endswith(".md")]
        return out

    def type_files(self) -> list[Path]:
        return sorted((self.path / "types").glob("*.type.yaml")) if (self.path / "types").exists() else []

    def glob(self, sub: str, pattern: str) -> list[Path]:
        d = self.path / sub
        return sorted(d.glob(pattern)) if d.exists() else []

    def skills(self) -> list[Path]:
        return sorted(p.parent for p in self.glob("skills", "*/SKILL.md"))

    def lock_path(self) -> Path:
        return self.path / "knowledge.lock"

    def read_lock(self) -> dict:
        p = self.lock_path()
        return (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}

    def save_manifest(self) -> None:
        raw = dict(self.manifest)
        raw["dependencies"] = [{"name": d.name, "version": d.version} for d in self.dependencies]
        (self.path / "repo.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
        self.manifest = raw


# ── registry ─────────────────────────────────────────────────────────────────

class Registry:
    """Local, private package registry: `<root>/<name>/<version>/` directories, never edited in place."""

    def __init__(self, root: Path):
        self.root = root

    def packages(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        if not self.root.exists():
            return out
        for d in sorted(self.root.iterdir()):
            if d.is_dir():
                vs = [v.name for v in d.iterdir() if (v / "repo.yaml").exists()]
                if vs:
                    out[d.name] = sorted(vs, key=vparse)
        return out

    def best(self, name: str, constraint: str = "*") -> str | None:
        ok = [v for v in self.packages().get(name, []) if satisfies(v, constraint)]
        return ok[-1] if ok else None

    def path(self, name: str, version: str) -> Path:
        return self.root / name / version

    def load(self, name: str, version: str) -> Repo:
        p = self.path(name, version)
        if not (p / "repo.yaml").exists():
            raise RepoError(f"package {name}@{version} is not in the registry")
        r = Repo.load(p, source="registry")
        r.writable = False
        return r

    def search(self, text: str = "") -> list[dict]:
        out = []
        for name, versions in self.packages().items():
            r = self.load(name, versions[-1])
            if text.lower() in (name + " " + r.description).lower():
                out.append({"name": name, "versions": versions, "latest": versions[-1], "description": r.description,
                            "exports": r.exports})
        return out

    def publish(self, src: Path) -> Repo:
        r = Repo.load(src)
        if r.kind != "package":
            raise RepoError(f"{r.name} is kind '{r.kind}'; only packages can be published")
        dst = self.path(r.name, r.version)
        if dst.exists():
            raise RepoError(f"{r.name}@{r.version} already published; bump the version (published versions are immutable)")
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".knowledge", "__pycache__", "knowledge.lock"))
        return self.load(r.name, r.version)


# ── workspace ────────────────────────────────────────────────────────────────

@dataclass
class Workspace:
    root: Path
    name: str = "workspace"
    repos: list[Repo] = field(default_factory=list)
    packages: dict[str, Repo] = field(default_factory=dict)    # resolved registry deps by name
    problems: list[dict] = field(default_factory=list)
    registry: Registry | None = None
    index_path: Path | None = None
    config: dict = field(default_factory=dict)

    @classmethod
    def open(cls, root: str | Path | None = None, registry: str | Path | None = None) -> "Workspace":
        """`registry` overrides the workspace's own (fixtures borrow the real registry this way)."""
        root = Path(root or os.environ.get("LIF_KNOWLEDGE_ROOT") or DEFAULT_ROOT).resolve()
        cfg_path = root / "workspace.yaml"
        cfg = (yaml.safe_load(cfg_path.read_text()) or {}) if cfg_path.exists() else {}
        reg = Path(registry) if registry else (root / cfg.get("registry", "registry"))
        if not registry and not reg.exists() and os.environ.get("LIF_KNOWLEDGE_REGISTRY"):
            reg = Path(os.environ["LIF_KNOWLEDGE_REGISTRY"])
        ws = cls(root=root, name=str(cfg.get("name", root.name)), config=cfg, registry=Registry(reg.resolve()))
        idx = os.environ.get("LIF_KNOWLEDGE_INDEX") or cfg.get("index", ".knowledge/index.db")
        ws.index_path = (root / idx).resolve() if idx != ":memory:" else None
        entries = cfg.get("repos") or ([{"path": "."}] if (root / "repo.yaml").exists() else [])
        for e in entries:
            e = {"path": e} if isinstance(e, str) else e
            p = (root / e["path"]).resolve()
            if not (p / "repo.yaml").exists():
                if e.get("optional"):
                    ws.problems.append({"code": "K020", "severity": "info", "path": str(e["path"]),
                                        "message": f"optional repo '{e['path']}' is not present (skipped)"})
                    continue
                ws.problems.append({"code": "K018", "severity": "error", "path": str(e["path"]),
                                    "message": f"repo '{e['path']}' has no repo.yaml"})
                continue
            ws.repos.append(Repo.load(p))
        ws.resolve()
        return ws

    def repo(self, name: str) -> Repo | None:
        return next((r for r in self.repos if r.name == name), None) or self.packages.get(name)

    def all_repos(self) -> list[Repo]:
        return self.repos + list(self.packages.values())

    def resolve(self) -> None:
        """Resolve every dependency (transitively) to a workspace repo or a registry version."""
        self.packages = {}
        local = {r.name for r in self.repos}
        queue = [(r, d) for r in self.repos for d in r.dependencies]
        while queue:
            owner, dep = queue.pop(0)
            if dep.name in local:
                continue
            lock = owner.read_lock().get("packages", {}) if owner.source == "workspace" else {}
            pinned = (lock.get(dep.name) or {}).get("version")
            have = self.packages.get(dep.name)
            if have is not None:
                if not satisfies(have.version, dep.version):
                    self.problems.append({"code": "K018", "severity": "error", "path": str(owner.path / "repo.yaml"),
                                          "message": f"{owner.name} needs {dep.name} {dep.version} but "
                                                     f"{have.version} is resolved for the workspace"})
                continue
            version = pinned or (self.registry.best(dep.name, dep.version) if self.registry else None)
            if not version:
                self.problems.append({"code": "K018", "severity": "error", "path": str(owner.path / "repo.yaml"),
                                      "message": f"unresolved dependency {dep.name} {dep.version} (not in registry)"})
                continue
            if pinned and not satisfies(pinned, dep.version):
                self.problems.append({"code": "K018", "severity": "error", "path": str(owner.lock_path()),
                                      "message": f"knowledge.lock pins {dep.name}@{pinned}, outside {dep.version}; "
                                                 f"run `knowledge package update {dep.name}`"})
            try:
                pkg = self.registry.load(dep.name, version)
            except RepoError as e:
                self.problems.append({"code": "K018", "severity": "error", "path": str(owner.lock_path()),
                                      "message": str(e)})
                continue
            if pinned:
                want = (lock.get(dep.name) or {}).get("hash")
                if want and want != tree_hash(pkg.path):
                    self.problems.append({"code": "K018", "severity": "error", "path": str(owner.lock_path()),
                                          "message": f"{dep.name}@{version} content does not match knowledge.lock "
                                                     f"hash (registry copy was modified)"})
            self.packages[dep.name] = pkg
            queue += [(pkg, d) for d in pkg.dependencies]

    def visible(self, repo: Repo) -> list[str]:
        """Namespaces a repo can see: itself, then its dependencies (transitively, breadth first)."""
        out, queue = [], [repo]
        while queue:
            r = queue.pop(0)
            for d in r.dependencies:
                dep = self.repo(d.name)
                if dep is not None and dep.name not in out:
                    out.append(dep.name)
                    queue.append(dep)
        return out
