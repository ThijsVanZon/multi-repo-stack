import sys

print(f"hello, {sys.argv[1] if len(sys.argv) > 1 else 'world'}")
