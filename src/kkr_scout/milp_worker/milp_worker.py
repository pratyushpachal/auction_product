"""Run one native HiGHS solve in an isolated process and return its result."""
import json
import sys

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

request = json.load(sys.stdin)
lower = np.array([float("-inf") if v is None else v for v in request["lower"]])
upper = np.array([float("inf") if v is None else v for v in request["upper"]])
result = milp(np.array(request["c"]), integrality=np.ones(len(request["c"])),
              bounds=Bounds(0, 1), constraints=LinearConstraint(np.array(request["matrix"]), lower, upper),
              options={"time_limit":15})
json.dump({"status": int(result.status), "x": None if result.x is None else result.x.tolist()}, sys.stdout, allow_nan=False)
