import os
import json
from datetime import datetime
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any

from src.db.database import SessionLocal, engine, Base
from src.db.models import (
    Node, NodeCrossReference, SOCHierarchy, ONetTaskStatement,
    ONetToolTechnology, SOCCrosswalk, OFLCWage, MSAMapping, ToolAuditLog
)

# Initialize database schemas
Base.metadata.create_all(bind=engine)

# Configure FastMCP
mcp = FastMCP(
    "immigration-navigator",
    version=os.getenv("MCP_SERVER_VERSION", "0.1.0"),
    description="MCP Server for U.S. Immigration Law and SOC/Wage Discovery"
)

# ==========================================
# Helpers
# ==========================================

UPL_DISCLAIMER = (
    "⚠️ DISCLAIMER: This is not legal advice. This information is "
    "provided for educational purposes only. Consult a qualified "
    "immigration attorney for advice specific to your situation."
)

def _wrap_legal_response(content: str) -> str:
    """Hardcoded UPL compliance. Pre-pends disclaimer to all legal text."""
    return f"{UPL_DISCLAIMER}\n\n---\n\n{content}"

def log_audit(tool_name: str, parameters: dict):
    """Log all tool calls to database for compliance."""
    try:
        with SessionLocal() as db:
            log = ToolAuditLog(tool_name=tool_name, parameters=parameters)
            db.add(log)
            db.commit()
    except Exception as e:
        # We don't want audit logging to break the actual request, but we should note it
        print(f"Failed to audit log {tool_name}: {e}")

# ==========================================
# SOC Discovery Tools
# ==========================================

@mcp.tool()
def get_soc_major_groups() -> List[Dict[str, Any]]:
    """
    Returns all ~23 major SOC groups with titles and descriptions.
    The Client LLM should filter to 2-3 most relevant and present to the user.
    """
    log_audit("get_soc_major_groups", {})
    with SessionLocal() as db:
        groups = db.query(SOCHierarchy).filter(SOCHierarchy.level == 0).all()
        return [{"soc_code": g.soc_code, "title": g.title, "description": g.description} for g in groups]

@mcp.tool()
def get_soc_children(parent_soc_code: str, include_tasks: bool = False) -> List[Dict[str, Any]]:
    """
    Returns direct children of a SOC node.
    If include_tasks=True, enriches with O*NET task statements and tools.
    """
    log_audit("get_soc_children", {"parent_soc_code": parent_soc_code, "include_tasks": include_tasks})
    with SessionLocal() as db:
        children = db.query(SOCHierarchy).filter(SOCHierarchy.parent_soc_code == parent_soc_code).all()
        results = []
        for c in children:
            data = {"soc_code": c.soc_code, "title": c.title, "description": c.description, "level": c.level}
            if include_tasks:
                tasks = db.query(ONetTaskStatement).filter(ONetTaskStatement.soc_code == c.soc_code).all()
                tools = db.query(ONetToolTechnology).filter(ONetToolTechnology.soc_code == c.soc_code).all()
                data["tasks"] = [t.task for t in tasks]
                data["tools"] = [t.t2_example for t in tools]
            results.append(data)
        return results

@mcp.tool()
def get_soc_details(soc_code: str) -> Dict[str, Any]:
    """
    Returns full O*NET profile for a confirmed SOC code.
    """
    log_audit("get_soc_details", {"soc_code": soc_code})
    with SessionLocal() as db:
        node = db.query(SOCHierarchy).filter(SOCHierarchy.soc_code == soc_code).first()
        if not node:
            return {"error": f"SOC {soc_code} not found."}
        
        tasks = db.query(ONetTaskStatement).filter(ONetTaskStatement.soc_code == soc_code).all()
        tools = db.query(ONetToolTechnology).filter(ONetToolTechnology.soc_code == soc_code).all()
        
        return {
            "soc_code": node.soc_code,
            "title": node.title,
            "description": node.description,
            "tasks": [t.task for t in tasks],
            "tools": [t.t2_example for t in tools]
        }

# ==========================================
# Wage & MSA Tools
# ==========================================

@mcp.tool()
def resolve_msa(zip_code_or_city: str) -> List[Dict[str, Any]]:
    """
    Maps user location input to exact OFLC MSA Area name(s).
    """
    log_audit("resolve_msa", {"zip_code_or_city": zip_code_or_city})
    with SessionLocal() as db:
        # Basic search: try zip first, then city
        msas = db.query(MSAMapping).filter(
            (MSAMapping.zip_code == zip_code_or_city) | 
            (MSAMapping.city_name.ilike(f"%{zip_code_or_city}%"))
        ).limit(10).all()
        
        if not msas:
            return [{"error": f"Could not find MSA for '{zip_code_or_city}'"}]
            
        return [{"zip": m.zip_code, "city": m.city_name, "state": m.state_abbr, "msa_area": m.msa_area} for m in msas]

@mcp.tool()
def get_wage_data(soc_code: str, msa_area: str) -> Dict[str, Any]:
    """
    Returns Level 1-4 wages for the SOC x MSA combination.
    Uses crosswalk if O*NET code differs from OFLC code.
    """
    log_audit("get_wage_data", {"soc_code": soc_code, "msa_area": msa_area})
    with SessionLocal() as db:
        # Check crosswalk first
        crosswalk = db.query(SOCCrosswalk).filter(SOCCrosswalk.onet_soc_code == soc_code).first()
        query_soc = crosswalk.oflc_soc_code if crosswalk else soc_code
        
        wages = db.query(OFLCWage).filter(
            OFLCWage.soc_code == query_soc,
            OFLCWage.msa_area == msa_area
        ).all()
        
        if not wages:
            return {"error": f"No wage data found for SOC '{query_soc}' in MSA '{msa_area}'."}
            
        levels = {}
        for w in wages:
            levels[f"Level {w.wage_level}"] = {
                "hourly": str(w.hourly_wage), 
                "yearly": str(w.yearly_wage)
            }
            
        return {
            "oflc_soc_code": query_soc,
            "soc_title": wages[0].soc_title,
            "msa_area": msa_area,
            "wages": levels,
            "crosswalk_used": bool(crosswalk)
        }

# ==========================================
# Legal Navigation Tools
# ==========================================
#  
@mcp.tool()
def read_legal_node(node_id: Optional[int] = None) -> str:
    """
    Returns children of a node for traversal (or roots if None).
    Includes title, summary, citation stub.
    UPL disclaimer hardcoded into response.
    """
    log_audit("read_legal_node", {"node_id": node_id})
    with SessionLocal() as db:
        query = db.query(Node)
        if node_id is None:
            query = query.filter(Node.parent_id == None)
        else:
            query = query.filter(Node.parent_id == node_id)
            
        nodes = query.order_by(Node.id).all()
        
        if not nodes:
            return _wrap_legal_response(f"No children found for node_id {node_id}")
            
        results = []
        for n in nodes:
            results.append({
                "node_id": n.id,
                "title": n.title,
                "summary": n.summary,
                "citation": n.citation,
                "is_leaf": n.full_text is not None
            })
            
        # Return as pretty json string wrapped in UPL disclaimer
        return _wrap_legal_response(json.dumps(results, indent=2))

@mcp.tool()
def get_legal_leaf(node_id: int) -> str:
    """
    Returns full legal text + citation + cross_references (using their node_IDs).
    UPL disclaimer hardcoded into response.
    """
    log_audit("get_legal_leaf", {"node_id": node_id})
    with SessionLocal() as db:
        node = db.query(Node).filter(Node.id == node_id).first()
        
        if not node:
            return _wrap_legal_response(f"Node {node_id} not found.")
            
        if not node.full_text:
            return _wrap_legal_response(f"Node {node_id} is not a leaf node. Use read_legal_node to traverse deeper.")
            
        # Get OUTBOUND cross references (where this node points TO other nodes)
        xrefs = db.query(NodeCrossReference).filter(NodeCrossReference.source_node_id == node_id).all()
        
        result = {
            "node_id": node.id,
            "title": node.title,
            "citation": node.citation,
            "text": node.full_text,
            "metadata": node.metadata_,
            "cross_references": [
                {"target_node_id": x.target_node_id, "reference_text": x.reference_text} 
                for x in xrefs
            ]
        }
        
        return _wrap_legal_response(json.dumps(result, indent=2))

@mcp.tool()
def get_legal_citations(node_ids: List[int]) -> str:
    """
    Batch-returns citation paths for multiple nodes. Used to compile a final answer.
    """
    log_audit("get_legal_citations", {"node_ids": node_ids})
    with SessionLocal() as db:
        nodes = db.query(Node).filter(Node.id.in_(node_ids)).all()
        citations = {n.id: n.citation for n in nodes if n.citation}
        return _wrap_legal_response(json.dumps(citations, indent=2))

@mcp.tool()
def get_server_info() -> Dict[str, Any]:
    """
    Returns server metadata including data freshness stamp and source info.
    """
    indexed_date = os.getenv("DATA_INDEXED_DATE", "2026-03-14")
    return {
        "status": "online",
        "data_freshness": f"All results are based on data indexed on {indexed_date}.",
        "sources": [
            "O*NET 30.2 Database",
            "OFLC 2026 Wage Database",
            "Federal Register XML (H-1B Rule)" ,
            "USCIS Policy Manual (Vol 2, 6, 7)"
        ],
        "compliance": "Strict UPL Hardcoding Enabled"
    }

if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport == "sse":
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")
