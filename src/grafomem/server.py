import os, json, base64, struct
import structlog
from mcp.server.fastmcp import FastMCP
from grafomem.mcp import MCP_CONTENT_TYPE

logger = structlog.get_logger("grafomem.server")

def create_server(registry_dir: str) -> FastMCP:
    mcp = FastMCP("Grafomem CSO Registry")
    logger.info("server.init", registry_dir=registry_dir)
    
    loaded_count = 0
    errors = []
    
    if os.path.exists(registry_dir):
        for fname in os.listdir(registry_dir):
            if not fname.endswith(".gfm"): continue
            sid = fname[:-4]
            path = os.path.join(registry_dir, fname)
            
            try:
                with open(path, "rb") as f: b = f.read()
                # minimal parse
                if len(b) < 4 or b[:4] != b"GFM1":
                    logger.warning("server.skip.invalid_magic", file=fname)
                    errors.append((fname, "invalid magic"))
                    continue
                o = 4
                if len(b) < o + 4:
                    logger.warning("server.skip.truncated", file=fname)
                    errors.append((fname, "truncated header length"))
                    continue
                hl = struct.unpack("<I", b[o:o+4])[0]; o += 4
                if len(b) < o + hl:
                    logger.warning("server.skip.truncated", file=fname)
                    errors.append((fname, "truncated header JSON"))
                    continue
                h = json.loads(b[o:o+hl].decode("utf-8"))
                
                desc = json.dumps({
                    "model_id": h.get("model_id"),
                    "capabilities": sorted(h.get("capabilities", []))
                })
                
                def make_reader(p=path):
                    def read_cso() -> str:
                        with open(p, "rb") as f2: b2 = f2.read()
                        o2 = 4
                        hl2 = struct.unpack("<I", b2[o2:o2+4])[0]; o2 += 4
                        h2 = json.loads(b2[o2:o2+hl2].decode("utf-8"))
                        res = {
                            "contentType": MCP_CONTENT_TYPE,
                            "bytes": base64.b64encode(b2).decode("utf-8"),
                            "descriptor": {
                                "model_id": h2.get("model_id"),
                                "capabilities": sorted(h2.get("capabilities", [])),
                                "consent": h2.get("consent", {}),
                                "hash": os.path.basename(p)[:-4]
                            }
                        }
                        return json.dumps(res)
                    return read_cso
                
                mcp.resource(
                    f"grafomem://cso/{sid}",
                    name=f"CSO {sid}",
                    description=desc
                )(make_reader(path))
                loaded_count += 1
            except Exception as e:
                logger.error("server.error", file=fname, error=str(e))
                errors.append((fname, str(e)))
                
    logger.info("server.ready", resources=loaded_count, errors=len(errors))
    return mcp

if __name__ == "__main__":
    registry_dir = os.environ.get("LETHE_REGISTRY_DIR", ".")
    app = create_server(registry_dir)
    app.run()
