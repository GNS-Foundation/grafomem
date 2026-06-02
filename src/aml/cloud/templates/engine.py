import yaml
import logging
from typing import Any, Dict, List

from aml.cloud.world_model import WorldModelService, WorldModelError

logger = logging.getLogger("grafomem.cloud.templates")

class TemplateEngine:
    """
    Parses and instantiates Ontological Templates (YAML) into the WorldModelService.
    Supports Option B: Additive Sync (UPSERTs based on type_id).
    """

    def __init__(self, world_model: WorldModelService):
        self.world_model = world_model

    def parse_yaml(self, yaml_content: str) -> Dict[str, Any]:
        """Parse the template YAML."""
        try:
            return yaml.safe_load(yaml_content)
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML template: {e}")

    def install_template(self, tenant_id: str, yaml_content: str) -> Dict[str, Any]:
        """
        Compiles the YAML and registers the types into the tenant's World Model.
        Returns the list of created/updated types.
        """
        template = self.parse_yaml(yaml_content)
        
        # Basic validation
        if template.get("apiVersion") != "grafomem.com/v1alpha1" or template.get("kind") != "OntologyTemplate":
            raise ValueError("Unsupported template version or kind.")

        metadata = template.get("metadata", {})
        template_name = metadata.get("name", "unknown")
        logger.info(f"Installing template '{template_name}' for tenant '{tenant_id}'")

        installed_types = []

        # 1. Register Classes (Object Types)
        classes = template.get("classes", [])
        for cls in classes:
            name = cls.get("name")
            if not name:
                continue
            spec = {
                "properties": cls.get("properties", {}),
                "template_source": template_name
            }
            res = self.world_model.register_type(tenant_id, "object", name, spec)
            installed_types.append(res)

        # 2. Register Relationships (Link Types)
        relationships = template.get("relationships", [])
        for rel in relationships:
            name = rel.get("name")
            from_type = rel.get("from")
            to_type = rel.get("to")
            if not name or not from_type or not to_type:
                continue
            spec = {
                "from_type": from_type,
                "to_type": to_type,
                "cardinality": rel.get("cardinality", "many_to_many"),
                "template_source": template_name
            }
            res = self.world_model.register_type(tenant_id, "link", name, spec)
            installed_types.append(res)

        # 3. Register Axioms (Action Types)
        axioms = template.get("axioms", [])
        for axiom in axioms:
            name = axiom.get("name")
            subject = axiom.get("subject")
            if not name:
                continue
            spec = {
                "operation": f"worldmodel.action.{name}",
                "required_trust_tier": axiom.get("required_trust_tier", "untrusted"),
                "subject_type": subject,
                "template_source": template_name
            }
            res = self.world_model.register_type(tenant_id, "action", name, spec)
            installed_types.append(res)

        return {
            "template": template_name,
            "version": metadata.get("version"),
            "installed_types_count": len(installed_types)
        }
