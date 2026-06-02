import os
from typing import List, Dict

TEMPLATES_DIR = os.path.dirname(__file__)

def list_templates() -> List[Dict[str, str]]:
    """Returns a list of available templates with metadata."""
    templates = []
    for filename in os.listdir(TEMPLATES_DIR):
        if filename.endswith(".yaml"):
            template_id = filename[:-5]
            # Fast read just the metadata
            with open(os.path.join(TEMPLATES_DIR, filename), "r") as f:
                content = f.read()
                # Basic parsing for metadata without full YAML parse
                import yaml
                parsed = yaml.safe_load(content)
                metadata = parsed.get("metadata", {})
                templates.append({
                    "id": template_id,
                    "name": metadata.get("name", template_id),
                    "version": metadata.get("version", "1.0"),
                    "description": metadata.get("description", "")
                })
    return templates

def get_template(template_id: str) -> str:
    """Returns the YAML string for a template ID."""
    path = os.path.join(TEMPLATES_DIR, f"{template_id}.yaml")
    if not os.path.exists(path):
        raise ValueError(f"Template '{template_id}' not found.")
    with open(path, "r") as f:
        return f.read()
