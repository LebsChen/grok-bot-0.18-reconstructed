"""Replace the runtime tarball's vendored devbox_bot with this plugin source."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import stat
import tarfile
from pathlib import Path, PurePosixPath


def _archive_name(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]
    return name


def _is_devbox_bot(name: str) -> bool:
    name = _archive_name(name)
    return name == "devbox_bot" or name.startswith("devbox_bot/")


def _source_files(source: Path) -> list[Path]:
    files = [
        path for path in source.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and ".pytest_cache" not in path.parts
        and path.suffix != ".pyc"
    ]
    if not any(path.suffix == ".py" for path in files):
        raise ValueError(f"no Python modules found under {source}")
    return sorted(files, key=lambda path: path.relative_to(source).as_posix())


def _source_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_source(tar: tarfile.TarFile, source: Path,
                  files: list[Path]) -> dict[str, str]:
    directories = {PurePosixPath("devbox_bot")}
    for path in files:
        relative = PurePosixPath(path.relative_to(source).as_posix())
        parent = relative.parent
        while str(parent) != ".":
            directories.add(PurePosixPath("devbox_bot") / parent)
            parent = parent.parent

    for relative in sorted(directories, key=lambda item: (len(item.parts), str(item))):
        info = tarfile.TarInfo(relative.as_posix() + "/")
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = 0
        tar.addfile(info)

    hashes: dict[str, str] = {}
    for path in files:
        relative = path.relative_to(source).as_posix()
        name = f"devbox_bot/{relative}"
        info = tarfile.TarInfo(name)
        info.size = path.stat().st_size
        info.mode = stat.S_IMODE(path.stat().st_mode)
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = 0
        with path.open("rb") as stream:
            tar.addfile(info, stream)
        hashes[name] = _source_digest(path)
    return hashes


def repack_runtime_pack(base_pack: Path, source: Path,
                        output_pack: Path) -> dict[str, str]:
    base_pack = base_pack.resolve()
    source = source.resolve()
    output_pack = output_pack.resolve()
    if base_pack == output_pack:
        raise ValueError("base and output packs must be different files")
    if not source.is_dir():
        raise ValueError(f"source directory does not exist: {source}")

    files = _source_files(source)
    output_pack.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_pack.with_name(output_pack.name + ".tmp")
    try:
        with (
            temporary.open("wb") as raw_output,
            gzip.GzipFile(
                filename="", mode="wb", fileobj=raw_output, mtime=0,
            ) as compressed,
            tarfile.open(
                fileobj=compressed, mode="w",
                format=tarfile.PAX_FORMAT,
            ) as output,
        ):
            with tarfile.open(base_pack, mode="r:gz") as base:
                for member in base:
                    if _is_devbox_bot(member.name):
                        continue
                    content = (base.extractfile(member)
                               if member.isfile() else None)
                    try:
                        output.addfile(member, content)
                    finally:
                        if content is not None:
                            content.close()
            hashes = _write_source(output, source, files)
        os.replace(temporary, output_pack)
    finally:
        temporary.unlink(missing_ok=True)

    packed: dict[str, str] = {}
    with tarfile.open(output_pack, mode="r:gz") as rebuilt:
        for member in rebuilt:
            name = _archive_name(member.name)
            if not member.isfile() or not _is_devbox_bot(name):
                continue
            stream = rebuilt.extractfile(member)
            if stream is None:
                raise ValueError(f"could not read packed file: {name}")
            packed[name] = hashlib.sha256(stream.read()).hexdigest()
    if packed != hashes:
        raise ValueError("packed devbox_bot files differ from the source tree")

    digest = hashlib.sha256(output_pack.read_bytes()).hexdigest()
    sidecar = Path(str(output_pack) + ".sha256")
    sidecar.write_text(f"{digest}  {output_pack.name}\n", encoding="utf-8")
    return hashes


def main() -> None:
    default_source = Path(__file__).resolve().parents[1] / "devbox_bot"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_pack", type=Path)
    parser.add_argument("output_pack", type=Path)
    parser.add_argument("--source", type=Path, default=default_source)
    args = parser.parse_args()
    hashes = repack_runtime_pack(args.base_pack, args.source, args.output_pack)
    digest = hashlib.sha256(args.output_pack.read_bytes()).hexdigest()
    print(f"pack={args.output_pack} sha256={digest} devbox_bot_files={len(hashes)}")


if __name__ == "__main__":
    main()
