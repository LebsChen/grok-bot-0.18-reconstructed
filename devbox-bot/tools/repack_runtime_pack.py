"""Refresh the Grok Bot runtime tarball with current plugin components."""

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


def _is_box_exec_daemon(name: str) -> bool:
    return _archive_name(name) == "opt-sand/box-exec-daemon/main.cjs"


def _is_start_box(name: str) -> bool:
    return _archive_name(name) == "opt-sand/grok-bot-box/start-box.sh"


def _is_provision(name: str) -> bool:
    return _archive_name(name) == "opt-sand/grok-bot-box/provision.py"


def _is_exec_daemon_shim(name: str) -> bool:
    return _archive_name(name) == "opt-sand/exec-daemon/exec-daemon"


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


def _write_box_exec_daemon(tar: tarfile.TarFile,
                           box_exec_daemon: Path) -> str:
    name = "opt-sand/box-exec-daemon/main.cjs"
    info = tarfile.TarInfo(name)
    info.size = box_exec_daemon.stat().st_size
    info.mode = 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    with box_exec_daemon.open("rb") as stream:
        tar.addfile(info, stream)
    return hashlib.sha256(box_exec_daemon.read_bytes()).hexdigest()


def _write_start_box(tar: tarfile.TarFile, start_box: Path) -> str:
    info = tarfile.TarInfo("opt-sand/grok-bot-box/start-box.sh")
    info.size = start_box.stat().st_size
    info.mode = 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    with start_box.open("rb") as stream:
        tar.addfile(info, stream)
    return hashlib.sha256(start_box.read_bytes()).hexdigest()


def _write_provision(tar: tarfile.TarFile, provision: Path) -> str:
    info = tarfile.TarInfo("opt-sand/grok-bot-box/provision.py")
    info.size = provision.stat().st_size
    info.mode = 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    with provision.open("rb") as stream:
        tar.addfile(info, stream)
    return hashlib.sha256(provision.read_bytes()).hexdigest()


def _write_exec_daemon_shim(tar: tarfile.TarFile,
                            exec_daemon_shim: Path) -> str:
    info = tarfile.TarInfo("opt-sand/exec-daemon/exec-daemon")
    info.size = exec_daemon_shim.stat().st_size
    info.mode = 0o755
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    with exec_daemon_shim.open("rb") as stream:
        tar.addfile(info, stream)
    return hashlib.sha256(exec_daemon_shim.read_bytes()).hexdigest()


def repack_runtime_pack(base_pack: Path, source: Path,
                        output_pack: Path,
                        box_exec_daemon: Path) -> dict[str, str]:
    base_pack = base_pack.resolve()
    source = source.resolve()
    output_pack = output_pack.resolve()
    box_exec_daemon = box_exec_daemon.resolve()
    if base_pack == output_pack:
        raise ValueError("base and output packs must be different files")
    if not source.is_dir():
        raise ValueError(f"source directory does not exist: {source}")
    if not box_exec_daemon.is_file():
        raise ValueError(
            f"box exec-daemon file does not exist: {box_exec_daemon}")
    start_box = Path(__file__).resolve().parents[1] / "box" / "start-box.sh"
    if not start_box.is_file():
        raise ValueError(f"start-box script does not exist: {start_box}")
    provision = Path(__file__).resolve().parents[1] / "box" / "provision.py"
    if not provision.is_file():
        raise ValueError(f"provisioning listener does not exist: {provision}")
    exec_daemon_shim = (
        Path(__file__).resolve().parents[1] / "box" / "exec-daemon")
    if not exec_daemon_shim.is_file():
        raise ValueError(
            f"exec-daemon shim does not exist: {exec_daemon_shim}")

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
                    if (_is_devbox_bot(member.name)
                            or _is_box_exec_daemon(member.name)
                            or _is_start_box(member.name)
                            or _is_provision(member.name)
                            or _is_exec_daemon_shim(member.name)):
                        continue
                    content = (base.extractfile(member)
                               if member.isfile() else None)
                    try:
                        output.addfile(member, content)
                    finally:
                        if content is not None:
                            content.close()
            hashes = _write_source(output, source, files)
            daemon_hash = _write_box_exec_daemon(output, box_exec_daemon)
            start_box_hash = _write_start_box(output, start_box)
            provision_hash = _write_provision(output, provision)
            shim_hash = _write_exec_daemon_shim(output, exec_daemon_shim)
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
    with tarfile.open(output_pack, mode="r:gz") as rebuilt:
        daemon_members = [
            member for member in rebuilt
            if _is_box_exec_daemon(member.name) and member.isfile()
        ]
        if len(daemon_members) != 1:
            raise ValueError("packed box exec-daemon is missing or duplicated")
        daemon_member = daemon_members[0]
        daemon_stream = rebuilt.extractfile(daemon_member)
        if (daemon_member.mode != 0o644 or daemon_stream is None
                or hashlib.sha256(daemon_stream.read()).hexdigest()
                != daemon_hash):
            raise ValueError("packed box exec-daemon differs from its source")
    with tarfile.open(output_pack, mode="r:gz") as rebuilt:
        start_box_members = [
            member for member in rebuilt
            if _is_start_box(member.name) and member.isfile()
        ]
        if len(start_box_members) != 1:
            raise ValueError("packed start-box script is missing or duplicated")
        start_box_member = start_box_members[0]
        start_box_stream = rebuilt.extractfile(start_box_member)
        if (start_box_member.mode != 0o644 or start_box_stream is None
                or hashlib.sha256(start_box_stream.read()).hexdigest()
                != start_box_hash):
            raise ValueError("packed start-box script differs from its source")
    with tarfile.open(output_pack, mode="r:gz") as rebuilt:
        provision_members = [
            member for member in rebuilt
            if _is_provision(member.name) and member.isfile()
        ]
        if len(provision_members) != 1:
            raise ValueError(
                "packed provisioning listener is missing or duplicated")
        provision_member = provision_members[0]
        provision_stream = rebuilt.extractfile(provision_member)
        if (provision_member.mode != 0o644 or provision_stream is None
                or hashlib.sha256(provision_stream.read()).hexdigest()
                != provision_hash):
            raise ValueError(
                "packed provisioning listener differs from its source")
    with tarfile.open(output_pack, mode="r:gz") as rebuilt:
        shim_members = [
            member for member in rebuilt
            if _is_exec_daemon_shim(member.name) and member.isfile()
        ]
        if len(shim_members) != 1:
            raise ValueError(
                "packed exec-daemon shim is missing or duplicated")
        shim_member = shim_members[0]
        shim_stream = rebuilt.extractfile(shim_member)
        if (shim_member.mode != 0o755 or shim_stream is None
                or hashlib.sha256(shim_stream.read()).hexdigest()
                != shim_hash):
            raise ValueError("packed exec-daemon shim differs from its source")

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
    parser.add_argument("--box-exec-daemon", type=Path, required=True)
    args = parser.parse_args()
    hashes = repack_runtime_pack(
        args.base_pack, args.source, args.output_pack,
        args.box_exec_daemon)
    digest = hashlib.sha256(args.output_pack.read_bytes()).hexdigest()
    print(f"pack={args.output_pack} sha256={digest} devbox_bot_files={len(hashes)}")


if __name__ == "__main__":
    main()
