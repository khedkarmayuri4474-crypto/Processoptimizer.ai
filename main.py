from __future__ import annotations
import io, base64, traceback, logging, os
from typing import Any, Dict, List
from fastapi import FastAPI, UploadFile, File, HTTPException, Body, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import pandas as pd
from optimizer import optimize_blend
from datetime import datetime

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("BlendAPI")

# Initialize FastAPI app
app = FastAPI(title="ProcessOptimizer.ai – Blend API")

# CORS middleware configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Get the absolute path to the frontend build directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_BUILD_PATH = os.path.normpath(os.path.join(BASE_DIR, "..", "frontend", "build"))

# Debugging - print the paths
logger.info(f"Base directory: {BASE_DIR}")
logger.info(f"Frontend build path: {FRONTEND_BUILD_PATH}")

# Check if build directory exists
if os.path.exists(FRONTEND_BUILD_PATH):
    logger.info(f"Build directory exists: {os.listdir(FRONTEND_BUILD_PATH)}")
    
    # Serve assets from the assets directory
    assets_path = os.path.join(FRONTEND_BUILD_PATH, "assets")
    if os.path.exists(assets_path):
        app.mount("/assets", StaticFiles(directory=assets_path), name="assets")
        logger.info(f"Assets mounted at /assets from {assets_path}")
    
    # Serve the React app's index.html for all routes
    @app.get("/", response_class=HTMLResponse)
    async def serve_frontend():
        """Serve the frontend application"""
        index_path = os.path.join(FRONTEND_BUILD_PATH, "index.html")
        if os.path.exists(index_path):
            return FileResponse(index_path)
        else:
            logger.error(f"Index.html not found at {index_path}")
            return HTMLResponse("<h1>Frontend not built</h1><p>Please run 'npm run build' in the frontend directory</p>")

    @app.get("/{full_path:path}", response_class=HTMLResponse)
    async def catch_all(full_path: str, request: Request):
        """Catch all other routes and serve the frontend"""
        # Don't interfere with API routes
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        
        # Check if the requested file exists
        file_path = os.path.join(FRONTEND_BUILD_PATH, full_path)
        if os.path.exists(file_path) and os.path.isfile(file_path):
            return FileResponse(file_path)
        
        # Otherwise, serve index.html for React Router
        index_path = os.path.join(FRONTEND_BUILD_PATH, "index.html")
        if os.path.exists(index_path):
            return FileResponse(index_path)
        else:
            logger.error(f"Index.html not found at {index_path}")
            return HTMLResponse("<h1>Frontend not built</h1><p>Please run 'npm run build' in the frontend directory</p>")
else:
    logger.warning(f"Frontend build directory not found at {FRONTEND_BUILD_PATH}")

# Add middleware for debugging
@app.middleware("http")
async def debug_middleware(request: Request, call_next):
    logger.info(f"Request: {request.method} {request.url}")
    response = await call_next(request)
    logger.info(f"Response: {response.status_code}")
    return response

# ---------- helpers

def _alias_lookup(cols: List[str], *aliases: str) -> str | None:
    s = {c.strip().lower(): c for c in cols}
    for a in aliases:
        k = str(a).strip().lower()
        if k in s:
            return s[k]
    return None

def parse_excel_bytes(buf: bytes) -> Dict[str, Any]:
    """
    Parses an .xlsx with sheets:
      - Components (or 'components', 'blend', 'component')
      - Property_Specs (optional; alias: 'properties', 'constraints')
    Returns:
      {"components": [...], "property_specs": [...]}
    """
    try:
        logger.info(f"Parsing Excel file of size: {len(buf)} bytes")
        xls = pd.ExcelFile(io.BytesIO(buf), engine="openpyxl")
    except Exception as e:
        error_msg = f"Could not open .xlsx (openpyxl): {e}"
        if "file is not a zip file" in str(e).lower():
            error_msg = "Uploaded file is not a valid Excel file"
        logger.error(f"Excel parsing error: {error_msg}")
        raise HTTPException(status_code=400, detail=error_msg)

    sheet_names = [s.lower() for s in xls.sheet_names]
    logger.info(f"Found sheets: {xls.sheet_names}")
    
    comp_sheet_name = None
    for nm in xls.sheet_names:
        if str(nm).strip().lower() in ("components", "component", "blend", "blend_components"):
            comp_sheet_name = nm
            break
    if not comp_sheet_name:
        # Try partial matching
        for sheet in xls.sheet_names:
            if any(name in sheet.lower() for name in ["component", "blend"]):
                comp_sheet_name = sheet
                break
    
    if not comp_sheet_name:
        # fallback first sheet
        comp_sheet_name = xls.sheet_names[0]
        logger.warning(f"No component sheet found, using first sheet: {comp_sheet_name}")

    try:
        comp_df = pd.read_excel(xls, sheet_name=comp_sheet_name, engine="openpyxl").fillna("")
    except Exception as e:
        error_msg = f"Error reading sheet '{comp_sheet_name}': {e}"
        logger.error(error_msg)
        raise HTTPException(status_code=400, detail=error_msg)
        
    comp_cols = list(comp_df.columns)
    logger.info(f"Columns in component sheet: {comp_cols}")

    c_name = _alias_lookup(comp_cols, "name", "component", "componentname", "blendname")
    if not c_name:
        error_msg = "Components sheet missing 'Name/Component' column."
        logger.error(error_msg)
        raise HTTPException(status_code=400, detail=error_msg)

    # Optional common columns
    c_cost = _alias_lookup(comp_cols, "cost", "unit cost", "price")
    c_min  = _alias_lookup(comp_cols, "min", "min_vol", "minimum")
    c_max  = _alias_lookup(comp_cols, "max", "max_vol", "maximum")
    c_av   = _alias_lookup(comp_cols, "availability", "avail", "available")
    c_den  = _alias_lookup(comp_cols, "density")

    # Any other column is considered a property
    fixed = {c_name, c_cost, c_min, c_max, c_av, c_den}
    prop_cols = [c for c in comp_cols if c not in fixed and c is not None]

    components: List[Dict[str, Any]] = []
    for _, row in comp_df.iterrows():
        name = str(row[c_name]).strip() if c_name else ""
        if not name:
            continue
        def fget(col):
            if not col: return None
            v = row[col]
            return None if v == "" else v
        rec = {
            "name": name,
            "cost": fget(c_cost),
            "min": fget(c_min),
            "max": fget(c_max),
            "availability": fget(c_av),
            "density": fget(c_den),
        }
        for pc in prop_cols:
            key = str(pc).strip().lower()
            rec[key] = fget(pc)
        components.append(rec)

    # Property specs sheet (optional)
    spec_sheet = None
    for nm in xls.sheet_names:
        if str(nm).strip().lower() in ("property_specs", "properties", "constraints"):
            spec_sheet = nm
            break

    property_specs: List[Dict[str, Any]] = []
    if spec_sheet:
        try:
            ps = pd.read_excel(xls, sheet_name=spec_sheet, engine="openpyxl").fillna("")
            ps_cols = list(ps.columns)
            col_prop   = _alias_lookup(ps_cols, "property", "prop", "name")
            col_min    = _alias_lookup(ps_cols, "min", "min_value", "minimum")
            col_max    = _alias_lookup(ps_cols, "max", "max_value", "maximum")
            col_linear = _alias_lookup(ps_cols, "linear", "islinear")
            col_basis  = _alias_lookup(ps_cols, "basis")
            col_idx    = _alias_lookup(ps_cols, "index_formula", "index", "indexformula")
            col_rev    = _alias_lookup(ps_cols, "reverse_formula", "reverse", "reverseformula")

            for _, row in ps.iterrows():
                pname = str(row[col_prop]).strip() if col_prop else ""
                if not pname:
                    continue
                def g(col): return None if not col else (None if row[col] == "" else row[col])
                property_specs.append({
                    "property": pname.lower(),
                    "min": g(col_min),
                    "max": g(col_max),
                    "linear": str(g(col_linear)).lower() not in ("false","0","no","none"),
                    "basis": str(g(col_basis) or "volume").lower(),
                    "index_formula": g(col_idx) or "",
                    "reverse_formula": g(col_rev) or "",
                })
        except Exception as e:
            logger.warning(f"Error reading property specs sheet: {e}")

    logger.info(f"Parsed {len(components)} components and {len(property_specs)} property specs")
    return {"components": components, "property_specs": property_specs}

# ---------- routes

@app.get("/health")
def health():
    """Comprehensive health check"""
    status = {"ok": True, "dependencies": {}, "timestamp": datetime.now().isoformat()}
    
    # Check critical imports
    try:
        import pulp
        status["dependencies"]["pulp"] = "healthy"
    except ImportError:
        status["ok"] = False
        status["dependencies"]["pulp"] = "missing"
    
    try:
        import pandas
        status["dependencies"]["pandas"] = "healthy"
    except ImportError:
        status["ok"] = False
        status["dependencies"]["pandas"] = "missing"
        
    try:
        import openpyxl
        status["dependencies"]["openpyxl"] = "healthy"
    except ImportError:
        status["ok"] = False
        status["dependencies"]["openpyxl"] = "missing"
    
    return status

@app.get("/api/test-connectivity")
def test_connectivity():
    """Test endpoint for frontend to verify connectivity"""
    return {
        "message": "Backend is reachable",
        "timestamp": datetime.now().isoformat(),
        "version": "1.0"
    }

@app.get("/api/template.xlsx")
def template_xlsx():
    """
    Generate a simple template on the fly so users to download.
    """
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        pd.DataFrame([{
            "Name": "Component 1", "Cost": 10, "Min": 0, "Max": 2000, "Availability": 1000, "Density": 0.75,
            "RON": 90, "MON": 82, "RVP": 8, "Sulfur": 0.4
        }]).to_excel(w, index=False, sheet_name="Components")
        pd.DataFrame([{
            "Property": "RON", "Min": 90, "Max": 95, "Linear": True, "Basis": "volume", "Index_Formula": "", "Reverse_Formula": ""
        }]).to_excel(w, index=False, sheet_name="Property_Specs")
    buf.seek(0)
    
    # Add Content-Disposition header to force download
    headers = {"Content-Disposition": "attachment; filename=blend_template.xlsx"}
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers=headers)

@app.post("/api/parse/excel")
async def parse_excel(file: UploadFile = File(...)):
    logger.info(f"Received file: {file.filename}")
    try:
        filename = file.filename or "uploaded.xlsx"
        if not filename.lower().endswith(".xlsx"):
            logger.warning(f"Invalid file type: {filename}")
            raise HTTPException(status_code=400, detail="Please upload an .xlsx file (not .xls).")
        raw = await file.read()
        logger.info(f"File size: {len(raw)} bytes")
        if not raw or len(raw) < 100:
            logger.warning("File appears empty or too small")
            raise HTTPException(status_code=400, detail="Uploaded file appears empty or invalid.")
        data = parse_excel_bytes(raw)
        if not data.get("components"):
            logger.warning("No components found in Excel")
            raise HTTPException(status_code=400, detail="No components found in Excel.")
        return data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"PARSE ERROR TRACEBACK:\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Parse crashed: {e}")

def _to_float(x):
    if x is None or x == "":
        return None
    try:
        return float(x)
    except Exception:
        return None

def _call_opt(components: List[Dict[str, Any]],
              specs: Dict[str, Any],
              property_specs: List[Dict[str, Any]]):
    # Normalize numeric strings -> floats
    comps_norm = []
    for c in components:
        cc = dict(c)
        for k in ("cost","min","max","availability","density"):
            cc[k] = _to_float(cc.get(k))
        # property columns left as-is; optimizer will coerce
        comps_norm.append(cc)

    specs_norm = {
        "volume": float(specs.get("volume", 0) or 0),
        "time_limit": int(specs.get("time_limit", 300) or 300),
        "gap": float(specs.get("gap", 0.0) or 0.0),
        "threads": int(specs.get("threads", 1) or 1),
    }

    props_norm = []
    for p in property_specs or []:
        props_norm.append({
            "property": str(p.get("property") or "").lower(),
            "min": _to_float(p.get("min")),
            "max": _to_float(p.get("max")),
            "linear": str(p.get("linear", True)).lower() not in ("false","0","no","none"),
            "basis": str(p.get("basis", "volume") or "volume").lower(),
            "index_formula": p.get("index_formula") or "",
            "reverse_formula": p.get("reverse_formula") or "",
        })

    return optimize_blend(comps_norm, specs_norm, props_norm)

@app.post("/api/optimize/excel")

async def optimize_excel(volume: float,
                         time_limit: int = 300,
                         gap: float = 0.0,
                         threads: int = 1,
                         file: UploadFile = File(...)):
    try:
        logger.info(f"Starting optimization with volume={volume}")
        raw = await file.read()
        parsed = parse_excel_bytes(raw)
        res = _call_opt(parsed["components"],
                        {"volume": volume, "time_limit": time_limit, "gap": gap, "threads": threads},
                        parsed.get("property_specs", []))

        # Build report - ensure we have blend data
        if res.get("blend"):
            blend_rows = [{"Component": k, "Volume": v} for k, v in res["blend"].items()]
            comp_df = pd.DataFrame(blend_rows)
            
            # Create properties dataframe
            props_data = {}
            if res.get("properties"):
                for prop_name, prop_value in res["properties"].items():
                    props_data[prop_name] = prop_value
            
            props_df = pd.DataFrame([props_data])

            out = io.BytesIO()
            with pd.ExcelWriter(out, engine="openpyxl") as w:
                comp_df.to_excel(w, index=False, sheet_name="Blend")
                if not props_df.empty:
                    props_df.to_excel(w, index=False, sheet_name="Properties")
            out.seek(0)
            b64 = base64.b64encode(out.read()).decode("utf-8")
            res["report_xlsx_b64"] = b64
        else:
            res["report_xlsx_b64"] = ""
            
        logger.info(f"Optimization completed with status: {res.get('status')}")
        return res
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"OPTIMIZE ERROR TRACEBACK:\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Optimization failed: {e}")
@app.post("/api/optimize/json")
def optimize_json(payload: Dict[str, Any] = Body(...)):
    try:
        logger.info("Starting JSON optimization")
        comps = payload.get("components") or []
        props = payload.get("property_specs") or []
        specs = payload.get("specs") or {}
        res = _call_opt(comps, specs, props)
        logger.info(f"JSON optimization completed with status: {res.get('status')}")
        return JSONResponse(res)
    except Exception as e:
        logger.error(f"OPTIMIZE(JSON) TRACEBACK:\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Optimization failed: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)