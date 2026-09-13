"""Resolve, hash and archive the gameplay inputs loaded by the supported source-tree engine."""

import hashlib
import json
import platform
import zipfile
from functools import lru_cache
from importlib import metadata
from pathlib import Path

from zero_ad_bench.engine import DEFAULT_ENGINE


ROOT = Path(__file__).resolve().parents[3]
GAMEPLAY_PATHS = ("mod.json", "simulation", "globalscripts", "maps", "art/terrains")
LFS_POINTER = b"version https://git-lfs.github.com/spec/v1\n"


def runtime_versions():
    """Record the Python runtime and installed provider SDK used by the runner."""
    versions = {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
    }
    for package in ("anthropic", "httpx"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = None
    return versions


@lru_cache(maxsize=50000)
def _digest(path, _size, _mtime_ns, _ctime_ns):
    sha = hashlib.sha256()
    pointer = False
    with Path(path).open("rb") as handle:
        for index, chunk in enumerate(iter(lambda: handle.read(1 << 20), b"")):
            if index == 0:
                pointer = chunk.startswith(LFS_POINTER)
            sha.update(chunk)
    return sha.hexdigest(), pointer


def digest(path):
    path = Path(path).resolve()
    stat = path.stat()
    return _digest(str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def mod_roots(mods, sources=None, engine=DEFAULT_ENGINE):
    """Ordered roots: copied explicit mods override the executable's data/mods distribution.

    Unknown installations must supply their roots explicitly; silently guessing would record
    a different distribution from the one executed. Cosmetic renderer/audio assets are outside
    the declared gameplay input scope; terrain definitions and all map files are included.
    """
    supplied = sources or {}
    distribution = Path(engine).resolve().parent.parent / "data/mods"
    roots = {}
    for name in dict.fromkeys(["mod", "public", *mods]):
        path = Path(supplied.get(name, distribution / name)).resolve()
        if not path.is_dir():
            raise ValueError(f"Cannot resolve loaded mod {name!r}; supply its mod root")
        roots[name] = path
    return roots


def input_files(mods, sources=None, engine=DEFAULT_ENGINE):
    files = {"runner/" + p.name: p for p in Path(__file__).parent.glob("*.py")}
    for name, root in mod_roots(mods, sources, engine).items():
        for relative in GAMEPLAY_PATHS:
            target = root / relative
            candidates = [target] if target.is_file() else target.rglob("*")
            for path in candidates:
                if path.is_file():
                    files[f"mods/{name}/{path.relative_to(root).as_posix()}"] = path
    return files


def capture(mods, sources=None, engine=DEFAULT_ENGINE):
    files = input_files(mods, sources, engine)
    hashes = {}
    pointers = []
    for name, path in sorted(files.items()):
        hashes[name], pointer = digest(path)
        if pointer:
            pointers.append(name)
    return {
        "scope": "gameplay_inputs_v1",
        "files": hashes,
        "lfs_pointers": pointers,
        "sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
    }


def archive(directory, expected, mods, sources=None, engine=DEFAULT_ENGINE):
    """Archive verified executed code/content once per experiment, including dirty source files."""
    target = Path(directory) / "inputs.zip"
    if target.is_file():
        with zipfile.ZipFile(target) as bundle:
            if sorted(bundle.namelist()) != sorted(expected["files"]):
                raise ValueError("Archived gameplay input file set differs from preregistration")
            for name, expected_hash in expected["files"].items():
                if hashlib.sha256(bundle.read(name)).hexdigest() != expected_hash:
                    raise ValueError(
                        f"Archived gameplay input differs from preregistration: {name}"
                    )
        return
    files = input_files(mods, sources, engine)
    if set(files) != set(expected["files"]):
        raise ValueError("Gameplay input file set changed after preregistration")
    temporary = target.with_suffix(".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for name, path in sorted(files.items()):
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() != expected["files"][name]:
                    raise ValueError(f"Gameplay input changed after preregistration: {name}")
                info = zipfile.ZipInfo(name)
                info.compress_type = zipfile.ZIP_DEFLATED
                bundle.writestr(info, data)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
