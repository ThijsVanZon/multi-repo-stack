"""Research-shaped fixture check: the estimator must recover the known slope of a deterministic synthetic
series exactly. It writes no evidence file, so its record shows the optional evidence as absent."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model import slope  # noqa: E402

xs = list(range(100))
estimate = slope(xs, [3 * x + 2 for x in xs])
print(f"estimated slope {estimate!r}; the synthetic series has slope 3.0")
sys.exit(0 if estimate == 3.0 else 1)
