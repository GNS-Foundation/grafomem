import re
import sys

def main():
    with open("src/aml/cloud/orchestrator.py", "r") as f:
        content = f.read()

    # 1. Add _CANON
    content = content.replace(
        'import psycopg\nfrom psycopg.rows import dict_row\n',
        'import functools\nimport psycopg\nfrom psycopg.rows import dict_row\n\n_CANON = functools.partial(json.dumps, sort_keys=True, separators=(",", ":"), default=str)\n'
    )

    # 2. Add columns to ensure_schema
    schema_addition = """
        try:
            conn.execute("ALTER TABLE orchestrator_agents ADD COLUMN IF NOT EXISTS system_prompt_enc TEXT;")
            conn.execute("ALTER TABLE orchestrator_steps ADD COLUMN IF NOT EXISTS input_text_enc TEXT;")
            conn.execute("ALTER TABLE orchestrator_steps ADD COLUMN IF NOT EXISTS retrieved_facts_enc TEXT;")
            conn.execute("ALTER TABLE orchestrator_steps ADD COLUMN IF NOT EXISTS governance_logs_enc TEXT;")
            conn.execute("ALTER TABLE orchestrator_steps ADD COLUMN IF NOT EXISTS raw_output_enc TEXT;")
            conn.execute("ALTER TABLE orchestrator_steps ADD COLUMN IF NOT EXISTS tool_calls_enc TEXT;")
            conn.execute("ALTER TABLE orchestrator_steps ADD COLUMN IF NOT EXISTS tool_results_enc TEXT;")
        except Exception as e:
            logger.warning(f"Could not alter tables for encryption columns: {e}")
"""
    content = content.replace(
        '        logger.info("Orchestrator schema ensured")',
        schema_addition + '        logger.info("Orchestrator schema ensured")'
    )

    # 3. create_agent
    content = re.sub(
        r'def create_agent\(\s*self,\s*tenant_id: str,\s*name: str,\s*role: AgentRole \| str,\s*model_id: str,\s*system_prompt: str,\s*\*,',
        'def create_agent(\n        self,\n        tenant_id: str,\n        name: str,\n        role: AgentRole | str,\n        model_id: str,\n        system_prompt: str,\n        *,\n        encryption: Any | None = None,',
        content
    )
    # Agent insert
    agent_insert = """
        enc_prompt = encryption.encrypt(system_prompt) if encryption else None
        db_prompt = "[ENCRYPTED]" if encryption else system_prompt

        conn = self._get_conn()
        conn.execute(
            "INSERT INTO orchestrator_agents "
            "(agent_id, tenant_id, name, role, description, model_id, fallback_models, "
            " system_prompt, system_prompt_enc, memory_stores, tools, max_steps, max_tokens, "
            " temperature, enabled, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                agent_id, tenant_id, name, role.value, description,
                model_id, json.dumps(fallbacks), db_prompt, enc_prompt,
                json.dumps(stores), json.dumps(tool_list),
                max_steps, max_tokens_per_step, temperature,
                enabled, now, now,
            ),
        )"""
    content = re.sub(
        r'        conn = self\._get_conn\(\)\n        conn\.execute\(\n            "INSERT INTO orchestrator_agents ".*?\),.*?        \)',
        agent_insert,
        content,
        flags=re.DOTALL
    )

    # 4. update_agent_system_prompt
    content = re.sub(
        r'def update_agent_system_prompt\(\s*self,\s*agent_id: str,\s*system_prompt: str\s*\) -> bool:',
        'def update_agent_system_prompt(self, agent_id: str, system_prompt: str, encryption: Any | None = None) -> bool:',
        content
    )
    content = re.sub(
        r'            "UPDATE orchestrator_agents SET system_prompt = %s, updated_at = %s WHERE agent_id = %s",\n            \(system_prompt, now, agent_id\),',
        '            "UPDATE orchestrator_agents SET system_prompt = %s, system_prompt_enc = %s, updated_at = %s WHERE agent_id = %s",\n            ("[ENCRYPTED]" if encryption else system_prompt, encryption.encrypt(system_prompt) if encryption else None, now, agent_id),',
        content
    )

    # 5. _row_to_agent
    content = re.sub(
        r'def _row_to_agent\(row: dict\[str, Any\]\) -> AgentDefinition:',
        'def _row_to_agent(row: dict[str, Any], encryption: Any | None = None) -> AgentDefinition:',
        content
    )
    content = content.replace(
        '            system_prompt=row["system_prompt"],',
        '            system_prompt=encryption.decrypt(row["system_prompt_enc"]) if encryption and row.get("system_prompt_enc") else row["system_prompt"],'
    )

    # 6. _row_to_step
    content = re.sub(
        r'def _row_to_step\(row: dict\[str, Any\]\) -> StepRecord:',
        'def _row_to_step(row: dict[str, Any], encryption: Any | None = None) -> StepRecord:',
        content
    )
    row_to_step_replace = """        def _load_json(val):
            if val is None:
                return None
            if isinstance(val, (list, dict)):
                return val
            return json.loads(val)

        input_text_enc = row.get("input_text_enc")
        input_text = encryption.decrypt(input_text_enc) if encryption and input_text_enc else row["input_text"]

        raw_output_enc = row.get("raw_output_enc")
        raw_output = encryption.decrypt(raw_output_enc) if encryption and raw_output_enc else row["raw_output"]

        facts_enc = row.get("retrieved_facts_enc")
        facts_str = encryption.decrypt(facts_enc) if encryption and facts_enc else row.get("retrieved_facts")

        gov_logs_enc = row.get("governance_logs_enc")
        gov_logs_str = encryption.decrypt(gov_logs_enc) if encryption and gov_logs_enc else row.get("governance_logs")

        calls_enc = row.get("tool_calls_enc")
        calls_str = encryption.decrypt(calls_enc) if encryption and calls_enc else row.get("tool_calls")

        results_enc = row.get("tool_results_enc")
        results_str = encryption.decrypt(results_enc) if encryption and results_enc else row.get("tool_results")

        return StepRecord(
            step_id=row["step_id"],
            workflow_id=row["workflow_id"],
            agent_id=row["agent_id"],
            tenant_id=row["tenant_id"],
            step_number=row["step_number"],
            input_text=input_text,
            retrieved_facts=_load_json(facts_str) or [],
            governance_allowed=row["governance_allowed"],
            governance_logs=_load_json(gov_logs_str) or [],
            model_id=row["model_id"],
            raw_output=raw_output,
            tool_calls=_load_json(calls_str) or [],
            tool_results=_load_json(results_str) or [],"""
    content = re.sub(
        r'        return StepRecord\(\n            step_id=row\["step_id"\],\n            workflow_id=row\["workflow_id"\],\n            agent_id=row\["agent_id"\],\n            tenant_id=row\["tenant_id"\],\n            step_number=row\["step_number"\],\n            input_text=row\["input_text"\],\n            retrieved_facts=row\["retrieved_facts"\] if isinstance\(row\["retrieved_facts"\], list\) else json\.loads\(row\["retrieved_facts"\]\),\n            governance_allowed=row\["governance_allowed"\],\n            governance_logs=row\["governance_logs"\] if isinstance\(row\["governance_logs"\], list\) else json\.loads\(row\["governance_logs"\]\),\n            model_id=row\["model_id"\],\n            raw_output=row\["raw_output"\],\n            tool_calls=row\["tool_calls"\] if isinstance\(row\["tool_calls"\], list\) else json\.loads\(row\["tool_calls"\]\),\n            tool_results=row\["tool_results"\] if isinstance\(row\["tool_results"\], list\) else json\.loads\(row\["tool_results"\]\),',
        row_to_step_replace,
        content
    )

    # 7. Add encryption to get_agent, list_agents, get_step, get_workflow_steps
    content = content.replace(
        'def get_agent(self, agent_id: str) -> AgentDefinition | None:',
        'def get_agent(self, agent_id: str, encryption: Any | None = None) -> AgentDefinition | None:'
    )
    content = content.replace(
        'return self._row_to_agent(row) if row else None',
        'return self._row_to_agent(row, encryption) if row else None'
    )
    content = content.replace(
        'def list_agents(self, tenant_id: str, include_disabled: bool = False) -> list[AgentDefinition]:',
        'def list_agents(self, tenant_id: str, include_disabled: bool = False, encryption: Any | None = None) -> list[AgentDefinition]:'
    )
    content = content.replace(
        'return [self._row_to_agent(r) for r in rows]',
        'return [self._row_to_agent(r, encryption) for r in rows]'
    )
    content = content.replace(
        'def get_step(self, step_id: str) -> StepRecord | None:',
        'def get_step(self, step_id: str, encryption: Any | None = None) -> StepRecord | None:'
    )
    content = content.replace(
        'return self._row_to_step(row) if row else None',
        'return self._row_to_step(row, encryption) if row else None'
    )
    content = content.replace(
        'def get_workflow_steps(self, workflow_id: str) -> list[StepRecord]:',
        'def get_workflow_steps(self, workflow_id: str, encryption: Any | None = None) -> list[StepRecord]:'
    )
    content = content.replace(
        'return [self._row_to_step(r) for r in rows]',
        'return [self._row_to_step(r, encryption) for r in rows]'
    )

    # 8. execute_step
    content = re.sub(
        r'def execute_step\(\n        self,\n        workflow_id: str,\n        agent_id: str,\n        input_text: str,\n        \*,',
        'def execute_step(\n        self,\n        workflow_id: str,\n        agent_id: str,\n        input_text: str,\n        *,\n        encryption: Any | None = None,',
        content
    )
    content = content.replace(
        'agent = self.get_agent(agent_id)',
        'agent = self.get_agent(agent_id, encryption)'
    )
    content = content.replace(
        'step = self._persist_step(',
        'step = self._persist_step(encryption=encryption,'
    )
    content = content.replace(
        '            "retrieved_facts": retrieved_facts,',
        '            "retrieved_facts": retrieved_facts,'
    )

    # 9. _persist_step
    # We must rewrite _persist_step to handle encryption and _CANON
    persist_step_replace = """    def _persist_step(self, **kwargs) -> StepRecord:
        \"\"\"Persist a step record to the database and return the StepRecord.\"\"\"
        encryption = kwargs.pop("encryption", None)
        
        # Canonical JSON
        facts_canon = _CANON(kwargs["retrieved_facts"])
        logs_canon = _CANON(kwargs["governance_logs"])
        calls_canon = _CANON(kwargs["tool_calls"])
        res_canon = _CANON(kwargs["tool_results"])

        enc_input = encryption.encrypt(kwargs["input_text"]) if encryption else None
        db_input = "[ENCRYPTED]" if encryption else kwargs["input_text"]

        enc_raw = encryption.encrypt(kwargs["raw_output"]) if encryption else None
        db_raw = "[ENCRYPTED]" if encryption else kwargs["raw_output"]

        enc_facts = encryption.encrypt(facts_canon) if encryption else None
        db_facts = "[]" if encryption else facts_canon

        enc_logs = encryption.encrypt(logs_canon) if encryption else None
        db_logs = "[]" if encryption else logs_canon

        enc_calls = encryption.encrypt(calls_canon) if encryption else None
        db_calls = "[]" if encryption else calls_canon

        enc_results = encryption.encrypt(res_canon) if encryption else None
        db_results = "[]" if encryption else res_canon

        conn = self._get_conn()
        conn.execute(
            "INSERT INTO orchestrator_steps "
            "(step_id, workflow_id, agent_id, tenant_id, step_number, "
            " input_text, retrieved_facts, governance_allowed, governance_logs, "
            " model_id, raw_output, tool_calls, tool_results, "
            " tokens_used, latency_ms, latency_governance_ms, latency_memory_ms, "
            " latency_llm_ms, latency_tools_ms, decision_id, parent_decision_id, signature, public_key, "
            " status, created_at, "
            " input_text_enc, retrieved_facts_enc, governance_logs_enc, raw_output_enc, tool_calls_enc, tool_results_enc) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s, "
            "        %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "        %s, %s, %s, %s, %s, %s)",
            (
                kwargs["step_id"],
                kwargs["workflow_id"],
                kwargs["agent_id"],
                kwargs["tenant_id"],
                kwargs["step_number"],
                db_input,
                db_facts,
                kwargs["governance_allowed"],
                db_logs,
                kwargs["model_id"],
                db_raw,
                db_calls,
                db_results,
                kwargs["tokens_used"],
                kwargs["latency_ms"],
                kwargs.get("latency_governance_ms", 0),
                kwargs.get("latency_memory_ms", 0),
                kwargs.get("latency_llm_ms", 0),
                kwargs.get("latency_tools_ms", 0),
                kwargs["decision_id"],
                kwargs.get("parent_decision_id"),
                kwargs["signature"],
                kwargs["public_key"],
                kwargs["status"].value,
                kwargs["created_at"],
                enc_input, enc_facts, enc_logs, enc_raw, enc_calls, enc_results
            ),
        )

        return StepRecord(
            step_id=kwargs["step_id"],
            workflow_id=kwargs["workflow_id"],
            agent_id=kwargs["agent_id"],
            tenant_id=kwargs["tenant_id"],
            step_number=kwargs["step_number"],
            input_text=kwargs["input_text"],
            retrieved_facts=kwargs["retrieved_facts"],
            governance_allowed=kwargs["governance_allowed"],
            governance_logs=kwargs["governance_logs"],
            model_id=kwargs["model_id"],
            raw_output=kwargs["raw_output"],
            tool_calls=kwargs["tool_calls"],
            tool_results=kwargs["tool_results"],
            tokens_used=kwargs["tokens_used"],
            latency_ms=kwargs["latency_ms"],
            latency_governance_ms=kwargs.get("latency_governance_ms", 0),
            latency_memory_ms=kwargs.get("latency_memory_ms", 0),
            latency_llm_ms=kwargs.get("latency_llm_ms", 0),
            latency_tools_ms=kwargs.get("latency_tools_ms", 0),
            decision_id=kwargs["decision_id"],
            parent_decision_id=kwargs.get("parent_decision_id"),
            signature=kwargs["signature"],
            public_key=kwargs["public_key"],
            status=kwargs["status"],
            created_at=kwargs["created_at"],
        )"""
    content = re.sub(
        r'    def _persist_step\(self, \*\*kwargs\) -> StepRecord:.*?(?=    def _row_to_agent)',
        persist_step_replace + '\n\n',
        content,
        flags=re.DOTALL
    )

    with open("src/aml/cloud/orchestrator.py", "w") as f:
        f.write(content)
    
if __name__ == "__main__":
    main()
