from __future__ import annotations
from typing import List, Dict, Any
from pulp import LpProblem, LpVariable, lpSum, LpMinimize, LpStatus, value
import pulp
import math
import logging
import ast

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("BlendOptimizer")

def _is_num(v):
    try:
        return v is not None and not (isinstance(v, str)) and math.isfinite(float(v))
    except Exception:
        return False

def _safe_eval(formula: str | None, xval: float | None):
    """Safer alternative to eval for formula evaluation"""
    if not formula:
        return None
    if xval is None:
        return None
    try:
        # Use ast.literal_eval for safer evaluation
        # This is a simple implementation - for complex formulas you might need a more robust parser
        # Replace 'x' with the actual value
        expr = formula.replace('x', str(xval))
        
        # For math functions, we need to handle them differently
        # This is a simplified approach - consider using a proper expression evaluator library
        if 'math.' in expr:
            # Replace math functions with their actual implementations
            expr = expr.replace('math.', '')
            # This is a very basic implementation - consider using a proper expression evaluator
            if 'log10' in expr:
                return math.log10(xval)
            elif 'exp' in expr:
                return math.exp(xval)
            # Add more math functions as needed
        
        # For simple arithmetic expressions
        return eval(expr, {"__builtins__": {}}, {})
    except Exception as e:
        logger.warning(f"Formula evaluation error: {e}, formula: {formula}, xval: {xval}")
        return None

def _calc(formula: str | None, xval: float | None):
    if not formula:
        return None
    if xval is None:
        return None
    try:
        safe = {"__builtins__": {}, "x": float(xval), "math": math}
        return eval(formula, safe)
    except Exception:
        # Try the safer alternative
        return _safe_eval(formula, xval)

def optimize_blend(components: List[Dict[str, Any]],
                   specs: Dict[str, Any],
                   property_specs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Optimizer used by both JSON and Excel endpoints.
    - components: [{name, cost, min, max, availability, density, <prop columns>}, ...]
    - specs: {"volume": float, "time_limit": int, "gap": float, "threads": int}
    - property_specs: [
        {"property": "ron", "min": 87, "max": 95, "linear": true, "basis": "volume",
         "index_formula": "", "reverse_formula": ""},
        ...
      ]
    """

    total_volume = float(specs.get("volume", 0) or 0)
    if total_volume <= 0:
        return {"status": "Error", "message": "Blend volume must be positive", "blend": {}, "total_cost": 0}

    # Fill defaults / sanitize
    comps = []
    for c in components:
        name = str(c.get("name") or "").strip()
        if not name:
            continue
        comps.append({
            "name": name,
            "cost": float(c.get("cost", 0) or 0),
            "min": float(c.get("min", 0) or 0),
            "max": float(c.get("max", total_volume) or total_volume),
            "availability": float(c.get("availability", total_volume) or total_volume),
            "density": float(c.get("density", 0.75) or 0.75),
            **{k: c[k] for k in c if k not in {"name","cost","min","max","availability","density"}}
        })

    prob = LpProblem("Blend_Optimization", LpMinimize)
    x = {c["name"]: LpVariable(f"x_{c['name']}", lowBound=0) for c in comps}

    # Objective: minimize total cost
    prob += lpSum([c["cost"] * x[c["name"]] for c in comps])

    # Blend volume
    prob += lpSum([x[c["name"]] for c in comps]) == total_volume

    # Component bounds & availability
    for c in comps:
        if _is_num(c.get("min")):
            prob += x[c["name"]] >= float(c["min"])
        if _is_num(c.get("max")):
            prob += x[c["name"]] <= float(c["max"])
        if _is_num(c.get("availability")):
            prob += x[c["name"]] <= float(c["availability"])

    # Property constraints
    for p in property_specs or []:
        pname = str(p.get("property") or "").strip().lower()
        if not pname:
            continue

        linear = str(p.get("linear", True)).lower() in ("true","1","yes","y")
        basis = str(p.get("basis", "volume") or "volume").lower()
        index_formula = str(p.get("index_formula") or "").strip()
        reverse_formula = str(p.get("reverse_formula") or "").strip()

        pmin = float(p["min"]) if _is_num(p.get("min")) else None
        pmax = float(p["max"]) if _is_num(p.get("max")) else None
        if pmin is None and pmax is None:
            continue

        terms = []
        denom_terms = []
        for c in comps:
            v = c.get(pname)
            if v is None:
                continue

            idx_val = v
            if not linear and index_formula:
                calc = _calc(index_formula, v)
                if calc is None:
                    continue
                idx_val = calc

            w = x[c["name"]] * (c["density"] if basis == "mass" else 1.0)
            terms.append(w * float(idx_val))
            denom_terms.append(w)

        if not terms:
            # No component carries this property → only allow constraint if both bounds absent
            continue

        expr = lpSum(terms)
        denom = lpSum(denom_terms)
        if pmin is not None:
            idx_min = pmin if linear else (_calc(index_formula, pmin) if index_formula else pmin)
            if idx_min is not None:
                prob += expr >= float(idx_min) * denom
        if pmax is not None:
            idx_max = pmax if linear else (_calc(index_formula, pmax) if index_formula else pmax)
            if idx_max is not None:
                prob += expr <= float(idx_max) * denom

    # Solve
    time_limit = int(specs.get("time_limit", 300) or 300)
    gap = float(specs.get("gap", 0.0) or 0.0)
    threads = int(specs.get("threads", 1) or 1)

    solver = pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit, gapRel=gap if gap > 0 else None)
    try:
        solver.threads = threads
    except Exception:
        pass

    prob.solve(solver)

    status_map = {
        pulp.LpStatusOptimal: "Optimal",
        pulp.LpStatusInfeasible: "Infeasible",
        pulp.LpStatusUnbounded: "Unbounded",
        pulp.LpStatusNotSolved: "Not Solved"
    }
    status = status_map.get(prob.status, "Unknown")

    res = {
        "status": status,
        "message": "",
        "blend": {},
        "total_cost": 0.0,
        "properties": {}
    }

    if prob.status == pulp.LpStatusOptimal:
        res["blend"] = {k: (x[k].value() or 0.0) for k in x}
        res["total_cost"] = float(value(prob.objective) or 0)

        # report properties
        for p in property_specs or []:
            pname = str(p.get("property") or "").strip().lower()
            if not pname:
                continue
            linear = str(p.get("linear", True)).lower() in ("true","1","yes","y")
            basis = str(p.get("basis", "volume") or "volume").lower()
            index_formula = str(p.get("index_formula") or "").strip()
            reverse_formula = str(p.get("reverse_formula") or "").strip()

            nume, deno = 0.0, 0.0
            for c in comps:
                v = c.get(pname)
                if v is None:
                    continue
                idx = v
                if not linear and index_formula:
                    calc = _calc(index_formula, v)
                    if calc is None:
                        continue
                    idx = calc
                flow = x[c["name"]].value() or 0.0
                w = flow * (c["density"] if basis == "mass" else 1.0)
                nume += w * float(idx)
                deno += w
            avg_idx = (nume / deno) if deno > 0 else None
            final_val = avg_idx
            if not linear and reverse_formula and avg_idx is not None:
                rev = _calc(reverse_formula, avg_idx)
                if rev is not None:
                    final_val = rev
            res["properties"][pname.upper()] = final_val

    return res