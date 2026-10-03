"""Implementation-shaped fixture check: build the program into build/greet.pyz, run the built program and
compare what it prints. The build output is disposable; the evidence names what was built and observed."""
import os
import subprocess
import sys
import zipapp

os.makedirs("build", exist_ok=True)
zipapp.create_archive("src/greet", "build/greet.pyz")
ran = subprocess.run([sys.executable, "-I", "build/greet.pyz", "fixture"], capture_output=True)
printed = ran.stdout.replace(b"\r\n", b"\n").decode("utf-8")
ok = ran.returncode == 0 and printed == "hello, fixture\n"
evidence = os.environ.get("MRS_EVIDENCE_FILE")
if evidence:
    with open(evidence, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"built build/greet.pyz ({os.path.getsize('build/greet.pyz')} bytes); it printed {printed!r}; "
                     + ("as expected\n" if ok else "expected 'hello, fixture\\n'\n"))
sys.exit(0 if ok else 1)
