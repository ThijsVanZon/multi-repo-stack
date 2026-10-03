"""Probe fixture check (test code): each mode exercises one collection boundary. Links and moved folders
point only inside the check's own disposable checkout."""
import json
import os
import subprocess
import sys

mode, args = sys.argv[1], sys.argv[2:]
evidence = os.environ["MRS_EVIDENCE_FILE"]
inside = os.path.abspath("untracked inside checkout.txt")


def write(data: bytes, path: str = evidence) -> None:
    with open(path, "wb") as handle:
        handle.write(data)


if mode == "exact":
    write("line one\r\nline two € with no final newline".encode("utf-8"))
elif mode == "limit":
    write(b"x" * 65536)
elif mode == "oversized":
    write(b"x" * 65537)
elif mode == "astral":
    write(("\U0001F600" * 16384).encode("utf-8"))  # 65536 bytes that escape to 196608 receipt bytes
elif mode == "not-utf8":
    write(b"\xff\xfe\x00")
elif mode == "directory":
    os.mkdir(evidence)
elif mode == "hardlink":
    write(b"linked\n", inside)
    os.link(inside, evidence)
elif mode == "symlink":
    write(b"linked\n", inside)
    os.symlink(inside, evidence)
elif mode == "escape":
    folder, elsewhere = os.path.dirname(evidence), os.path.abspath("escaped")
    os.rename(folder, folder + " moved")
    os.mkdir(elsewhere)
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(elsewhere, folder)
    else:
        os.symlink(elsewhere, folder)
    write(b"written through a replaced folder\n")
elif mode == "argv":
    write(json.dumps(args, ensure_ascii=False).encode("utf-8"))
elif mode == "mutate":
    with open("data.txt", "ab") as handle:
        handle.write(b"changed by the check\n")
    write(b"changed data.txt\n")
elif mode == "delete":
    os.remove("data.txt")
    write(b"deleted data.txt\n")
elif mode == "chmod":
    os.chmod("data.txt", 0o755)
    write(b"made data.txt executable\n")
elif mode == "outputs":
    os.makedirs(os.path.join("build", "out"))
    write(b"disposable\n", os.path.join("build", "out", "artifact.bin"))
    write(b"wrote untracked build outputs\n")
elif mode == "fail":
    write(b"the check found a problem\n")
    sys.exit(3)
elif mode == "touch":
    write(b"changed a selected source during the run\n", os.path.join(os.environ["FIXTURE_DIRTY"], "during run.txt"))
    write(b"touched\n")
elif mode == "head":
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout
    status = subprocess.run(["git", "status", "--porcelain=v1", "--untracked-files=all"], capture_output=True,
                            text=True, check=True).stdout
    write(f"HEAD {head.strip()}; status lines {len(status.splitlines())}\n".encode("ascii"))
elif mode == "advance":
    git = ["git", "--git-dir", os.environ["FIXTURE_TARGET"]]
    dev = subprocess.run(git + ["rev-parse", "refs/heads/dev"], capture_output=True, text=True, check=True).stdout
    new = subprocess.run(git + ["commit-tree", dev.strip() + "^{tree}", "-p", dev.strip(), "-m", "Competing work"],
                         capture_output=True, text=True, check=True).stdout
    subprocess.run(git + ["update-ref", "refs/heads/dev", new.strip(), dev.strip()], check=True)
    write(f"advanced dev to {new.strip()}\n".encode("ascii"))
elif mode != "absent":
    sys.exit(f"unknown probe mode {mode!r}")
